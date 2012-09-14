# /////////////////////////////////////////////////////////////////////////////
#                           Quiet Period CSMA CA MAC
#
# FuNLab
# University of Washington
# Morgan Redfield
#
# Implement a qpCSMA CA MAC. Note that this is not 802.11 (not even close).
# Currently the MAC just generates its own packets. Eventually this might be tied
# in with TUN/TAP.
#
# Addressing in this MAC is super kludgy. I'm basically just prepending a character
# to every packet that I send and using that as an address. The return address is
# apended to the end of the packet. I'm reserving 
# the characters 'x', 'y', and 'z' for special functions.
# 'x' is a broadcast packet (all packets are broadcast for now
# 'y', and 'z' are for future applications
#
# ToDo:
# I'm using RTS/CTS with broadcast packets, that can't work with more than 2 nodes
# figure out delay time parameters (minimize)
# /////////////////////////////////////////////////////////////////////////////

import time #for delay timing
import random #for random backoff
import threading #for main_loop
from sense_path import * #for spectrum sensing

# /////////////////////////////////////////////////////////////////////////////
#                           Carrier Sense MAC
# /////////////////////////////////////////////////////////////////////////////

class cs_mac(threading.Thread):
    """
    Reads packets from the application interface, and sends them to the PHY.
    Receives packets from the PHY via phy_rx_callback, and passes any data
    packets up to the application layer.
    """
    def __init__(self, options, callback):
        #thread set up
        threading.Thread.__init__(self)
        self._stop = threading.Event()
        self._done = False
        
        #logging variables
        self.verbose = options.verbose
        self.log_mac = options.log_mac
        
        # top block (access to PHY)
        self.tb = None             
        
        #MAC bookkeeping
        self.state = 0
        self.tx_tries = 0
        self.collisions = 0
        self.backoff = 0
        self.CWmin = options.cw_min #max(options.cw_min, int(options.quiet_period/options.backoff))
        self.packet_lifetime = options.packet_lifetime
        self.address = options.address
        self.err_array = None
                
        #control packet bookkeeping
        self.RTS_rcvd = False
        self.CTS_rcvd = False
        self.DAT_rcvd = False
        self.ACK_rcvd = False
        
        #delay time parameters
        #bus latency is also going to be a problem here
        self.SIFS_time = options.sifs
        self.DIFS_time = 2*options.backoff + options.sifs #options.difs
        self.ctl_pkt_time = options.ctl
        self.backoff_time_unit = options.backoff
        
        #spectrum sense parameters
        self.txrx_rate = options.samp_rate #transmit and receive bandwidth
        self.channel_rate = options.channel_rate #sense bandwidth of channel (not nec. 6 MHz)
        self.thresh_primary = options.thresh_primary
        self.thresh_second = options.thresh_second
        self.thresh_qp = options.thresh_qp
        self.sense_time = options.quiet_period
        self.quiet_period = int(self.sense_time/self.backoff_time_unit) #backoff units for qp
        #print "quiet period is ", self.quiet_period, " backoff units"
        self.qp_interval = options.qp_interval
        self.qp_counter = 0 #keep track of when we're at the qp interval
        self.old_freq = 0
        
        #used in calculating the avg power in dB
        self.k = 0
        
        #state machine bookkeeping variables
        self.tx_queue = []
        self.sender = None
        self.rx_callback = callback #what to do when we receive a data packet
        self.next_call = 0 #when to activate the MAC state machine again
        self.lock = threading.Lock()
        
        #test stuff, remove this before actually running the MAC
        #self.backoff_times = []
        #self.ready_to_backoff = 0
        #self.nominal_freq = options.tx_freq

    def run(self): #becomes a thread with mac.start() is called
        """
        Manages calls to the state machine at the proper times. This allows the 
        state machine to be fairly time agnostic.
        """
        try:
            times = []
            last_sense = time.clock()
            last_call = time.clock()
            #i = 0
            #do this until we get stopped by the host
            while not self.stopped(): # or len(self.tx_queue) > 0:
                #last_call = time.clock()
                #self.sense_current_freq()
                #times.append(time.clock() - last_call)
                
                #i += 1
                if self.next_call == "QP":
                    #it's time to sense the spectrum
                    self.next_call = self.sense_time
                    
                    #test code (measure time between senses)
                    times.append(time.clock() - last_sense)
                    last_sense = time.clock()
                    
                    occupied = self.sense_current_freq()
                    if occupied == 1: #one means a primary is using the channel
                        #change channels
                        new_freq = self.find_best_freq()
                    while self.next_call != "NOW" and (time.clock() - last_call < self.next_call):
                        #if sensing didn't take a long as we thought it would, wait for a while
                        pass
                if self.next_call == "NOW" or (self.next_call != 0 and 
                                               time.clock() - last_call > self.next_call):
                    #run the MAC state machine
                    self.state_machine()
                    last_call = time.clock()
            #Measurement code
            #print "avg sense time is ",  sum(times)/len(times)
            #print "max sense time is ", max(times)
            #print "avg backoff time slot is ", sum(self.backoff_times)/len(self.backoff_times)
            #print "max backoff time is ", max(self.backoff_times)
            mean = sum(times)/len(times)
            print
            print "avg time between sensing is: ", mean
            variance = 0
            for value in times:
                variance += (value - mean)**2
            variance = variance/(len(times) - 1)
            print "variance of sensing periods:  ", variance
            self._done = True
        except KeyboardInterrupt:
            self._done = True
                
    def stop(self):
        """
        Called from an outside process to stop the state machine.
        This function does not stop the MAC, it just alerts the MAC that it 
        should stop when it's most convenient.
        """
        self._stop.set()
    
    def stopped(self):
        """
        Returns a true/false value that determines whether 
        """
        return self._stop.isSet()
    
    def wait(self):
        """
        Waits until the state machine is stopped and then returns.
        """
        while not self._done:
            pass
            
    def set_flow_graph(self, tb):
        """
        Gives the MAC access to the PHY.
        
        @param tb: the top block of the GNURadio flowgraph representing the PHY
        """
        self.tb = tb
        mywindow = window.blackmanharris(self.tb.sense.fft_size)
        power = 0
        for tap in mywindow:
            power += tap*tap		
        self.k = -20*math.log10(self.tb.sense.fft_size)-10*math.log10(power/self.tb.sense.fft_size)
    
    def set_error_array(self, array):
    	self.err_array = array

    def new_packet(self, address, data):
        """
        Add a new packet to the queue.
        
        @param address: str the destination address of this packet
        @param data: str the data payload of the packet
        """
        self.tx_queue.append(str(address) + self.address + str(data))
        if self.next_call == 0:
            self.next_call = "NOW"
    
    def prep_to_sense(self, hold_freq):
        """
        Prepare the PHY to sense the spectrum.
        
        @param hold_freq: determines whether the PHY will switch channels as it senses.
        """
        #set frequency hold
        self.old_freq = self.tb.u_snk.get_center_freq()
        #print self.old_freq
        if not hold_freq:
            self.tb.sense.current_chan = self.tb.sense.num_channels
        #    self.tb.sense.next_freq = self.tb.sense.channels[0] #min_center_freq
        self.tb.sense.set_hold_freq(hold_freq)
        #stop rcving
        self.tb.rx_valve.set_enabled(False)
        #set rate
        self.tb.set_rate(self.channel_rate)
        #flush the queue
        self.tb.sense.msgq.flush()
        #start the spectrum sense
        self.tb.sense_valve.set_enabled(True)
    
    def prep_to_txrx(self):
        """
        Prepare the PHY to transmit and receive data
        """
        #done sensing
        self.tb.sense_valve.set_enabled(False)
        #flush the queue
        #self.tb.sense.msgq.flush()
        
        #reset rate
        self.tb.set_rate(self.txrx_rate)
        #start rcving
        self.tb.rx_valve.set_enabled(True)
        
    def find_best_freq(self):
        """
        Gather spectrum sense data and interpret it to find the frequency with the lowest noise
        floor.
        """
        #TODO
        #Ok, this algorithm totally sucks. It would be better if I could reliably sense the
        #simulated primary signal, but that interferes too much with adjacent channels, even if
        #those adjacent channels are like 10 MHz away. Fricken USRPs. 
        #I'm cheating and making the USRPs choose one of only two frequencies. As soon as I get
        #primary sensing more reliable, I'll switch back to the original frequency selection algorithm.
        self.prep_to_sense(False)
        frequencies = []#""
        power_levels = []
        while len(frequencies) == 0:
            i = 0
            while i < self.tb.sense.num_channels:
                i = i+1
                # Get the next message sent from the C++ code (blocking call).
                # It contains the center frequency and the mag squared of the fft
                m = parse_msg(self.tb.sense.msgq.delete_head())
            
                #fft_sum_db = 20*math.log10(sum(m.data)/m.vlen)
                temp_list = []
                for item in m.data:
                    temp_list.append(10*math.log10(item) + self.k)
                fft_sum_db = sum(temp_list)/m.vlen
                
                #print m.center_freq, fft_sum_db
                
                #this is a relic of using contiguous frequency bands rather than a set of
                #channels to select from
                #skip the first and last channels to account for noise at the edges
                #if ((int(m.center_freq) / 1000000) * 1000000) <= self.tb.sense.min_center_freq or ((int(m.center_freq) / 1000000) * 1000000) >= self.tb.sense.max_freq:#i == 1 or i >= self.tb.sense.num_channels:#
                #    pass
                #else: #elif fft_sum_db < best_freq[1] or best_freq[1] == 0:
                
                #the >200MHz thing is because sometimes m.center_freq is returned 
                #as 0 for some reason (bug somewhere?)
                if fft_sum_db < self.thresh_primary and m.center_freq > 200000000:
                    frequencies.append(m.center_freq)#= frequencies + "0"#
                    power_levels.append(fft_sum_db)
                #else:
                #    frequencies = frequencies + "1"

        #frequencies = frequencies + "\n"                    
        #log_file = open('sense_log.dat', 'a')
        #log_file.write(frequencies)
        #log_file.close()                
        #TODO: stop cheating
        if self.old_freq == self.tb.sense.channels[1]:
            best_freq = self.tb.sense.channels[4]
        else:
            best_freq = self.tb.sense.channels[1]
        #TODO: this is what it should be
        #best_freq = power_levels.index(min(power_levels)) #choose the best frequency
        #best_freq = frequencies[best_freq]
        
        print "\nchoosing frequency ", best_freq, " at time ", time.strftime("%X")
        #print 
        self.tb.set_freq(best_freq)
        self.prep_to_txrx()
        return best_freq
		
    def sense_current_freq(self):
        """
        sense the current channel and look for a primary user
        """
        self.prep_to_sense(True)
        #do the sensing
        m = parse_msg(self.tb.sense.msgq.delete_head())
        
        temp_list = []
        for item in m.data:
            temp_list.append(10*math.log10(item) + self.k)
        fft_sum_db = sum(temp_list)/m.vlen
        #print fft_sum_db
        
        #do threshold comparisons
        ret_val = 0
        if fft_sum_db > self.thresh_primary:
            #print fft_sum_db
            ret_val = 1
        elif fft_sum_db > self.thresh_second:
            ret_val = 2
        elif fft_sum_db > self.thresh_qp:
            ret_val = 3
        
        self.prep_to_txrx()
        
        return ret_val

    def phy_rx_callback(self, ok, payload):
        """
        Invoked by thread associated with PHY to pass received packet up.

        @param ok: bool indicating whether payload CRC was OK
        @param payload: contents of the packet (string)
        """
        #if the rcvd packet is empty or from this node, ignore it completely
        if len(payload) == 0 or (payload[1] == self.address):
            return

        #if self.verbose:
        #    print "Rx: ok = %r  len(payload) = %4d" % (ok, len(payload))
        if self.log_mac:
            log_file = open('csma_ca_mac_log.dat', 'w')
            if ok:
                log_file.write("RX:" + payload)
            else:
                log_file.write("RX - not ok")
            log_file.close()
            
        if ok:
            #the packet probably isn't corrupted and it's not from this node
            self.sender = payload[1]
            payload = payload[2:]
            if self.verbose:
                print "RX: ", payload, ", State: ", self.state, ", backoff: ", self.backoff, ", next call: ", self.next_call

            #is this a ctl packet?
            if len(payload) == 3:
                if payload == "RTS":
                    self.RTS_rcvd = True
                elif payload == "CTS":
                    self.CTS_rcvd = True
                elif payload == "ACK":
                    self.ACK_rcvd = True
                    self.rx_callback("T:" + payload)
                else: #wait, wut? it's a very short data packet?
                    self.DAT_rcvd = True
                    self.rx_callback("R:" + payload)
            else: #it's a data packet
                #print "received packet"
                self.DAT_rcvd = True
                if self.log_mac:
                    log_file = open('rx_data_log.dat', 'a')
                    log_file.write(payload + "\n")
                    log_file.close()
                self.rx_callback("R:" + payload)
                
            #we got a packet, make sure that the MAC state machine can do something with it
            #as soon as possible.
            self.next_call = "NOW"
    
    def state_machine(self):
        """
        State machine for qpCSMA/CA MAC.
        
        States
        0 - idle
        1 - RTS
        2 - DIFS
        3 - backoff
        4 - rts_sent
        5 - data_sent
        6 - cts_sent
        7 - ack_sent
        """
        #deal with the inputs to this function
        cb = True #was this a timer callback?
        if self.next_call == "NOW":
            cb = False
            
        self.lock.acquire()
        self.next_call = 0
            
        if self.verbose:
            print "S: ", self.state, ", L:", len(self.tx_queue)
        
        #take care of state transitions
        if self.state == 0: #idle state
            if self.RTS_rcvd: #someone wants to send to us
                self.RTS_rcvd = False
                if self.tb.carrier_sensed(): #they can't send because someone else is talking
                    #do nothing and remain in the idle state if we can't do a CTS
                    self.next_call = self.SIFS_time
                else: #they can send, so give them a CTS
                    if self.log_mac:
                         log_file = open('csma_ca_mac_log.dat', 'w')
                         log_file.write("TX:" + self.sender + self.address + "CTS")
                         log_file.close()
                    self.tb.txpath.send_pkt(self.sender + self.address + "CTS")
                    self.state = 6
                    self.next_call = self.SIFS_time + self.ctl_pkt_time
            elif len(self.tx_queue) > 0: #nobody wants to send to us and we want to send
                if not self.tb.carrier_sensed() and self.tx_tries < self.packet_lifetime:
                    self.state = 2
                    self.qp_counter = (self.qp_counter + 1) % self.qp_interval
                    self.next_call = self.DIFS_time
                elif self.tx_tries >= self.packet_lifetime:
                    if self.err_array != None:
                        self.err_array.append(1)
                    if self.verbose:
                        print "failed to send msg: "#, self.tx_queue[0]
                    if self.log_mac:
                        log_file = open('csma_ca_mac_log.dat', 'w')
                        log_file.write("TX: f - " + self.tx_queue[0])
                        log_file.close()
                    self.tx_queue.pop(0)
                    self.tx_tries = 0
                    if len(self.tx_queue) > 0:
                    	self.next_call = self.SIFS_time
                else:
                    self.next_call = self.SIFS_time
        elif self.state == 2: #done with DIFS, now backoff
            if cb and not self.tb.carrier_sensed(): #we're still ok, so keep backing off
                if self.backoff <= 0:
                    self.backoff = random.randrange(0, 2**self.tx_tries * self.CWmin, 1)
                #elif self.backoff > self.quiet_period:
                    #TODO: Make sure this way of dealing with backoff and qp fits Chitto's algorithm
                #    self.backoff = self.backoff - self.quiet_period
                self.state = 3
                if self.qp_counter == 0:
                    self.next_call = "QP"
                else:
                    self.next_call = self.backoff_time_unit
            else: #something happened (like we rx'd a packet), so go back to start state
                self.state = 0
                self.next_call = "NOW"
        elif self.state == 3: #backoff state
            if cb and not self.tb.carrier_sensed(): #we're still ok, so keep backing off
                self.backoff -= 1
                if self.backoff <= 0:
                    #self.ready_to_backoff = 0
                    if self.log_mac:
                        log_file = open('csma_ca_mac_log.dat', 'w')
                        log_file.write("TX:" + self.tx_queue[0][0] + self.address + "RTS")
                        log_file.close()
                    self.tb.txpath.send_pkt(self.tx_queue[0][0] + self.address + "RTS")
                    self.tx_tries += 1
                    self.state = 4
                    self.next_call = self.SIFS_time + self.ctl_pkt_time
                else:
                    #if self.ready_to_backoff != 0:
                        #self.backoff_times.append(time.clock() - self.ready_to_backoff)
                    #self.ready_to_backoff = time.clock()
                    
                    self.next_call = self.backoff_time_unit
            else: #something happened while we were backing off, go back to start state
                self.state = 0
                self.next_call = "NOW"
        elif self.state == 4: #RTS sent, wait for CTS
            if not self.CTS_rcvd: #timeout (or something)
                self.collisions += 1
                self.state = 0
                self.next_call = "NOW"
            else: #awesome, now we can send
                self.CTS_rcvd = False
                if self.log_mac:
                    log_file = open('csma_ca_mac_log.dat', 'w')
                    log_file.write("TX:" + self.tx_queue[0])
                    log_file.close()
                self.tb.txpath.send_pkt(self.tx_queue[0])
                self.state = 5
                self.next_call = self.SIFS_time + self.ctl_pkt_time
        elif self.state == 5: #data sent, wait for ACK
            if self.ACK_rcvd == True:
                #awesome, we're done
                self.tx_queue.pop(0)
                self.tx_tries = 0
                self.ACK_rcvd = False
            else: #we didn't get an ACK, so keep trying
                self.collisions += 1
            self.state = 0
            self.next_call = "NOW"
        elif self.state == 6: #RTS rcvd, sent CTS
            if self.DAT_rcvd:
                self.DAT_rcvd = False
                self.state = 7
                self.next_call = self.SIFS_time
            else:
                self.state = 0
                self.next_call = "NOW"
        elif self.state == 7: #data rcvd, send ACK
            if not self.tb.carrier_sensed():
                if self.log_mac:
                    log_file = open('csma_ca_mac_log.dat', 'w')
                    log_file.write("TX:" + self.sender + self.address + "ACK")
                    log_file.close()
                self.tb.txpath.send_pkt(self.sender + self.address + "ACK")
            self.state = 0
            self.next_call = "NOW"
        else:
            #something has gone terribly wrong, reset
            self.state = 0
            self.next_call = "NOW"
        self.lock.release()
        
    def add_options(normal, expert):
        """
        Adds MAC-specific options to the Options Parser
        """
        expert.add_option("", "--cw-min", type="int", default=2,
                          help="set minimum contention window (CWmin) [default=%default]")
        expert.add_option("", "--sifs", type="eng_float", default=.0002,
                          help="set SIFS time [default=%default]")
        #expert.add_option("", "--difs", type="eng_float", default=.005,
        #                  help="set DIFS time [default=%default]")
        expert.add_option("", "--ctl", type="eng_float", default=.04,
                          help="set control packet time [default=%default]")
        expert.add_option("", "--backoff", type="eng_float", default=.005,
                          help="set backoff time [default=%default]")
        expert.add_option("", "--packet-lifetime", type="int", default=5,
                          help="set number of attempts to send each packet [default=%default]")
        expert.add_option("", "--log-mac", action="store_true", default=False,
                          help="log all MAC layer tx/rx data [default=%default]")
        expert.add_option("-r", "--samp_rate", type="intx", default=800000,
                          help="set sample rate for USRP to SAMP_RATE [default=%default]")
        expert.add_option("", "--channel_rate", type="intx", default=4000000,
                          help="set channel rate for USRP spectrum sense to SAMP_RATE [default=%default]")
        expert.add_option("", "--thresh_primary", type="eng_float", default=-50,
                          help="set primary detection threshold [default=%default]")
        expert.add_option("", "--thresh_second", type="eng_float", default=-60,
                          help="set secondary detection threshold [default=%default]")
        expert.add_option("", "--thresh_qp", type="eng_float", default=-80,
                          help="set qpCSMA/CA detection threshold [default=%default]")
        expert.add_option("", "--quiet-period", type="eng_float", default=.03,
                          help="set quiet period length in seconds [default=%default]") 
        expert.add_option("", "--qp-interval", type="int", default=1,
                          help="set number of DIFS between qp [default=%default]") 
    # Make a static method to call before instantiation
    add_options = staticmethod(add_options)