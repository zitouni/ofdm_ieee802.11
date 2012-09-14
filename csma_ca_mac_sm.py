# /////////////////////////////////////////////////////////////////////////////
#                           Carrier Sense MAC
#
# FuNLab
# University of Washington
# Morgan Redfield
#
# Implement a CSMA CA MAC. Note that this is not 802.11 (not even close).
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
        
        #updated by Morgan Redfield on 2011 May 16
        self.verbose = options.verbose
        self.log_mac = options.log_mac
        self.err_array = None
        self.tb = None             # top block (access to PHY)
        
        #control packet bookkeeping
        self.RTS_rcvd = False
        self.CTS_rcvd = False
        self.DAT_rcvd = False
        self.ACK_rcvd = False
        
        #MAC bookkeeping
        self.state = 0
        self.tx_tries = 0
        self.collisions = 0
        self.backoff = 0
        self.CWmin = options.cw_min
        self.packet_lifetime = options.packet_lifetime
        self.address = options.address
        
        #delay time parameters
        #bus latency is also going to be a problem here
        self.SIFS_time = options.sifs
        self.DIFS_time = 2*options.backoff + options.sifs #options.difs
        self.ctl_pkt_time = options.ctl
        self.backoff_time_unit = options.backoff
        
        #state machine bookkeeping variables
        self.tx_queue = []
        self.sender = None
        self.rx_callback = callback
        self.next_call = 0
        self.lock = threading.Lock()

    def run(self):
        try:
            last_call = time.clock()
            while not self.stopped(): # or len(self.tx_queue) > 0:
                if self.next_call == "NOW" or (self.next_call != 0 and 
                                               time.clock() - last_call > self.next_call):
                    self.state_machine()
                    last_call = time.clock()
            self._done = True
        except KeyboardInterrupt:
            self._done = True
                
    def stop(self):
        self._stop.set()
    
    def stopped(self):
        return self._stop.isSet()
    
    def wait(self):
        while not self._done:
            pass
            
    def set_flow_graph(self, tb):
        self.tb = tb
    
    def set_error_array(self, array):
    	self.err_array = array

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
            self.sender = payload[1]
            payload = payload[2:]
            #question: is it possible that a packet sent from this function will 
            #interfere with a packet sent from the main_loop function?
            if self.verbose:
                print "RX: ", payload, ", State: ", self.state

            #is this a ctl packet?
            if len(payload) == 3:
                if payload == "RTS":
                    self.RTS_rcvd = True
                elif payload == "CTS":
                    self.CTS_rcvd = True
                elif payload == "ACK":
                    self.ACK_rcvd = True
                    self.rx_callback("T:" + payload)
                else: #wait, wut?
                    self.DAT_rcvd = True
                    self.rx_callback("R:" + payload)
            else: #it's a data packet
                self.DAT_rcvd = True
                log_file = open('rx_data_log.dat', 'a')
                log_file.write(payload + "\n")
                log_file.close()
                self.rx_callback("R:" + payload)
                
            self.next_call = "NOW"
    
    def new_packet(self, address, data):
        """
        Add a new packet to the queue.
        
        @param address: str the destination address of this packet
        @param data: str the data payload of the packet
        """
        self.tx_queue.append(str(address) + self.address + str(data))
        if self.next_call == 0:
            self.next_call = "NOW"
    
    def state_machine(self):
        """
        Main loop for MAC.
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
            if self.RTS_rcvd:
                self.RTS_rcvd = False
                if self.tb.carrier_sensed():
                    #do nothing and remain in the idle state if we can't do a CTS
                    self.next_call = self.SIFS_time
                else:
                    if self.log_mac:
                         log_file = open('csma_ca_mac_log.dat', 'w')
                         log_file.write("TX:" + self.sender + self.address + "CTS")
                         log_file.close()
                    self.tb.txpath.send_pkt(self.sender + self.address + "CTS")
                    self.state = 6
                    self.next_call = self.SIFS_time + self.ctl_pkt_time
                    #threading.Timer(self.ctl_pkt_time, self.state_machine).start()
            elif len(self.tx_queue) > 0:
                if not self.tb.carrier_sensed() and self.tx_tries < self.packet_lifetime:
                    self.state = 2
                    self.next_call = self.DIFS_time
                    #threading.Timer(self.DIFS_time, self.state_machine).start()
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
            if cb is True and not self.tb.carrier_sensed():
                if self.backoff == 0:
                    self.backoff = random.randrange(0, 2**self.tx_tries * self.CWmin, 1)
                self.state = 3
                self.next_call = self.backoff_time_unit
                #threading.Timer(self.backoff_time_unit, self.state_machine).timer.start()
            else:
                self.state = 0
                self.next_call = "NOW"#self.SIFS_time
        elif self.state == 3: #backoff state
            if cb and not self.tb.carrier_sensed():
                self.backoff -= 1
                if self.backoff <= 0:
                    if self.log_mac:
                        log_file = open('csma_ca_mac_log.dat', 'w')
                        log_file.write("TX:" + self.tx_queue[0][0] + self.address + "RTS")
                        log_file.close()
                    self.tb.txpath.send_pkt(self.tx_queue[0][0] + self.address + "RTS")
                    self.tx_tries += 1
                    self.state = 4
                    self.next_call = self.SIFS_time + self.ctl_pkt_time
                    #threading.Timer(self.SIFS_time + self.ctl_pkt_time, self.state_machine).start()
                else:
                    self.next_call = self.backoff_time_unit
                    #threading.Timer(self.backoff_time_unit, self.state_machine).start()
            else:
                self.state = 0
                self.next_call = "NOW"#self.SIFS_time
        elif self.state == 4: #RTS sent, wait for CTS
            if not self.CTS_rcvd: #timeout (or something)
                self.collisions += 1
                self.state = 0
                self.next_call = "NOW"#self.SIFS_time
            else: #awesome, now we can send
                self.CTS_rcvd = False
                if self.log_mac:
                    log_file = open('csma_ca_mac_log.dat', 'w')
                    log_file.write("TX:" + self.tx_queue[0])
                    log_file.close()
                self.tb.txpath.send_pkt(self.tx_queue[0])
                self.state = 5
                self.next_call = self.SIFS_time + self.ctl_pkt_time
                #threading.Timer(self.SIFS_time + self.ctl_pkt_time, self.state_machine).start()
        elif self.state == 5: #data sent, wait for ACK
            if self.ACK_rcvd == True:
                #awesome, we're done
                self.tx_queue.pop(0)
                self.tx_tries = 0
                self.ACK_rcvd = False
            else:
                self.collisions += 1
            self.state = 0
            self.next_call = "NOW"#self.SIFS_time
        elif self.state == 6: #RTS rcvd, sent CTS
            if self.DAT_rcvd:
                self.DAT_rcvd = False
                self.state = 7
                self.next_call = self.SIFS_time
                #threading.Timer(self.SIFS_time, self.state_machine).start()
            else:
                self.state = 0
                self.next_call = "NOW" #self.SIFS_time
        elif self.state == 7: #data rcvd, send ACK
            if not self.tb.carrier_sensed():
                if self.log_mac:
                    log_file = open('csma_ca_mac_log.dat', 'w')
                    log_file.write("TX:" + self.sender + self.address + "ACK")
                    log_file.close()
                self.tb.txpath.send_pkt(self.sender + self.address + "ACK")
            self.state = 0
            self.next_call = "NOW"#self.SIFS_time
        else:
            #something has gone terribly wrong, reset
            self.state = 0
            self.next_call = "NOW"#self.SIFS_time
        self.lock.release()
        
    def add_options(normal, expert):
        """
        Adds MAC-specific options to the Options Parser
        """
        expert.add_option("", "--cw-min", type="int", default=5,
                          help="set minimum contention window (CWmin) [default=%default]")
        expert.add_option("", "--sifs", type="eng_float", default=.0002,
                          help="set SIFS time [default=%default]")
        #expert.add_option("", "--difs", type="eng_float", default=.005,
        #                  help="set DIFS time [default=%default]")
        expert.add_option("", "--ctl", type="eng_float", default=.04,
                          help="set control packet time [default=%default]")
        expert.add_option("", "--backoff", type="eng_float", default=.0001,
                          help="set backoff time [default=%default]")
        expert.add_option("", "--packet-lifetime", type="int", default=5,
                          help="set number of attempts to send each packet [default=%default]")
        expert.add_option("", "--log-mac", action="store_true", default=False,
                          help="log all MAC layer tx/rx data [default=%default]")
    # Make a static method to call before instantiation
    add_options = staticmethod(add_options)