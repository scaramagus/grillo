"""
An audio modem, able to encode and decode data from/to audio. Internally uses chirp for
the modulation/demodulation, and unireedsolomon for error correction.
"""
import time
from datetime import datetime, timedelta

from chirpsdk import ChirpConnect, CallbackSet
from grillo import config


class MessageTooLongException(Exception):
    """
    Error raised when a message is too long to be sent.
    """
    pass


class MessagePartsLostException(Exception):
    """
    Error raised when part of a message can't be read.
    """
    pass

class MessageAckIsBroken(Exception):
    pass


class SinglePacketReceiver(CallbackSet):
    """
    A thing that can receive a single chirp packet, and stores it as an instance variable. It can
    also call a callback when the packet is received.
    """
    def __init__(self, callback=None):
        self.callback = callback
        self.packet = None

    def on_received(self, payload, channel):
        """
        Executed when chirp receives data.
        """
        self.packet = payload
        if self.callback is not None:
            self.callback(payload)


class ChainedMessageReceiver(CallbackSet):
    """
    A thing that can receive a chained message, and stores it as an instance variable. It can
    also call a callback when the message is received.
    """
    def __init__(self, modem, callback=None, reset_on_message=False, with_confirmation=False):
        self.modem = modem
        self.callback = callback
        self.reset_on_message = reset_on_message
        self.with_confirmation = with_confirmation

        self.reset_status()

    def reset_status(self):
        """
        Reset the receiving status.
        """
        self.message = None
        self.total_parts = None
        self.parts = {}

    def on_received(self, payload, channel):
        """
        Executed when chirp receives data.
        """
        if payload is not None:
            total_parts = payload[0]
            part_number = payload[1]
            message_part = payload[2:]

            if self.total_parts is None:
                # first part received!
                self.total_parts = total_parts

            self.parts[part_number] = message_part

            # finished receiving all the parts?
            if self.finished():
                self.message = self.combine()

                if self.callback is not None:
                    self.callback(self.message)

                if self.reset_on_message:
                    self.reset_status()

    def finished(self):
        """
        Is the message complete?
        """
        return len(self.parts.keys()) == self.total_parts

    def combine(self):
        """
        Concatenate all the message parts.
        """
        return b''.join(self.parts[part_number]
                        for part_number in range(self.total_parts))


class Modem:
    """
    An audio modem able to encode and decode data from/to audio. Internally uses chirp for
    the modulation/demodulation and error correction, but adding a layer that allows for messages
    longer than 32 bytes (sending multiple chirp messages for every grillo message).
    """
    DATA_LEN = 30

    def __init__(self, with_confirmation=False):
        self.chirp = ChirpConnect(
            key=config.CHIRP_APP_KEY,
            secret=config.CHIRP_APP_SECRET,
            config=config.CHIRP_APP_CONFIG,
        )
        self.chirp.start(send=True, receive=True)

        self.with_confirmation = with_confirmation

    def send_message(self, message):
        """
        Send a message as multiple packets.
        """
        chain_len = self._get_chain_len(len(message))
        if chain_len > 255:
            raise MessageTooLongException()

        packets_to_send = range(chain_len)
        while len(packets_to_send) > 0:
            self._send_packets(message, packets_to_send, chain_len)
            if self.with_confirmation:
                packets_to_send = self._get_packets_to_retry()
            else:
                break

    def _get_packets_to_retry(self):
        """
        Wait for the other end to inform which parts of a message it didn't receive.
        """
        packets_to_retry = []
        ack_msg = self.receive_packet(5)
        header = ack_msg[0]
        if header == 0:
            packets_to_retry = ack_msg[1:]
            return packets_to_retry
        else:
            raise MessageAckIsBroken()

    def _send_packets(self, message, packet_list, chain_len):
        """
        Send a message as multiple packets, one after the other.
        """
        for i in packet_list:
            packet = (
                bytes([chain_len, i])
                + message[self.DATA_LEN * i:self.DATA_LEN * (i + 1)])
            self.send_packet(packet)

    def send_packet(self, packet):
        """
        Send a single packet.
        """
        self.chirp.send(packet, blocking=True)

    def _get_chain_len(self, size):
        return size // self.DATA_LEN + 1

    def receive_packet(self, timeout=None):
        """
        Wait (blocking) for a single packet, and return it when received.
        """
        receiver = SinglePacketReceiver()
        self.chirp.set_callbacks(receiver)

        start = datetime.now()
        if timeout:
            timeout_delta = timedelta(seconds=timeout)

        while receiver.packet is None:
            time.sleep(0.1)

            if timeout:
                now = datetime.now()
                if now - start > timeout_delta:
                    break

        self.stop_listening()
        return receiver.packet

    def receive_message(self, timeout=None):
        """
        Wait (blocking) for a single message, and return it when received.
        """
        receiver = ChainedMessageReceiver(self, with_confirmation=self.with_confirmation)
        self.chirp.set_callbacks(receiver)

        start = datetime.now()
        if timeout:
            timeout_delta = timedelta(seconds=timeout)

        while receiver.message is None:
            time.sleep(0.1)

            if timeout:
                now = datetime.now()
                if now - start > timeout_delta:
                    break

        self.stop_listening()
        return receiver.message

    def listen_for_packets(self, callback):
        """
        Start listening for packets, calling a callback whenever a packet is received.
        """
        receiver = SinglePacketReceiver(callback)
        self.chirp.set_callbacks(receiver)

    def listen_for_messages(self, callback):
        """
        Start listening for messages, calling a callback whenever a packet is received.
        """
        receiver = ChainedMessageReceiver(self, callback, reset_on_message=True,
                                          with_confirmation=self.with_confirmation)
        self.chirp.set_callbacks(receiver)

    def stop_listening(self):
        """
        Stop using chirp to listen for packets.
        """
        self.chirp.set_callbacks(None)
