
from __future__ import with_statement

from mopidy import exceptions

import binascii
import logging
import serial
import struct
import threading
import time

logger = logging.getLogger(__name__)

# from mopidy_primare import primare_serial
# pt = primare_serial.PrimareTalker(port="/dev/ttyUSB0", input_source=None)
# pt.power_on()

# Primare documentation on their RS232 protocol writes this:
#  == Command structure ==
#  Commands are sent to the device using the following format, where each field
#  is one byte sent to the device:
#  <STX> <command> <variable> [<value>] <DLE> <ETX>
#  The <command> can be either 'W' for write or 'R' for read. The variable
#  table that follows specifies which variables supports which <command> types.
#  If verbose is active, the device will send replies on the following format
#  either when a command is received or when the variable in question is
#  changed on the device.
#  <STX> <variable> [<value>] <DLE> <ETX>
#  Note that the <value> field can contain several bytes of data for certain
#  commands.
#  == Command special chars ==
#  <STX> = 0x02
#  <DLE> = 0x10
#  <ETX> = 0x03
#  Write = 0x57 (ASCII: W)
#  Read = 0x52 (ASCII: R)
#  == Escape sequence ==
#  If any variable or value byte is equal to <DLE>, this byte must be sent
#  twice to avoid confusing this with end of message.
#  Protocol settings
#  Baud rate: 4800
#  Bits: 8
#  Stop bits: 1
#  Parity: None
#  == Example ==
#  The specific variables and commands will be defined later, here are
#  examples on what the commands looks like in bytes.
#  Command to toggle verbose setting. Command is write, variable is 13 (0x0d)
#  and value is 0.
#  0x02 0x57 0x0xd 0x00 0x10 0x03
POS_STX = slice(0, 1)
POS_DLE_ETX = slice(-2, None)
POS_CMD_VAR = slice(2, 3)
POS_REPLY_VAR = slice(1, 2)
POS_REPLY_DATA = slice(2, -2)
BYTE_STX = '\x02'
BYTE_WRITE = '\x57'
BYTE_READ = '\x52'
BYTE_DLE_ETX = '\x10\x03'

INDEX_CMD = 0
INDEX_VARIABLE = 1
INDEX_REPLY = 2
INDEX_WAIT = 3

PRIMARE_CMD = {
    'power_toggle': ['W', '0100', '01', True],
    'power_set': ['W', '81YY', '01YY', False],
    'input_set': ['W', '82YY', '02YY', True],
    'input_next': ['W', '0201', '02', True],
    'input_prev': ['W', '02FF', '02', True],
    'volume_set': ['W', '83YY', '03YY', True],
    'volume_up': ['W', '0301', '03', True],
    'volume_down': ['W', '03FF', '03', True],
    'balance_adjust': ['W', '04YY', '04', True],
    'balance_set': ['W', '84YY', '04YY', True],
    'mute_toggle': ['W', '0900', '09', True],
    'mute_set': ['W', '89YY', '09YY', True],
    'dim_cycle': ['W', '0A00', '0A', True],
    'dim_set': ['W', '0AYY', '0AYY', True],
    'verbose_toggle': ['W', '0D00', '0D', True],
    'verbose_set': ['W', '8DYY', '0DYY', True],
    'menu_toggle': ['W', '0E01', '0E', True],
    'menu_set': ['W', '8EYY', '0EYY', True],
    'remote_cmd': ['W', '0FYY', 'YY', True],
    'ir_input_toggle': ['W', '1200', '12', True],
    'ir_input_set': ['W', '92YY', '12YY', True],
    'recall_factory_settings': ['R', '1300', '', False],
    'inputname_current_get': ['R', '1400', '14YY', True],
    'inputname_specific_get': ['R', '94YY', '94YY', True],
    'manufacturer_get': ['R', '1500', '15', True],
    'modelname_get': ['R', '1600', '16', True],
    'swversion_get': ['R', '1700', '17', True]
}

PRIMARE_REPLY = {
    '01': '',  # power'
    '02': '',  # input'
    '03': '',  # volume'
    '04': '',  # balance'
    '09': '',  # mute'
    '0A': '',  # dim'
    '0D': '',  # verbose'
    '0E': '',  # menu'
#    'YY': '',  # remote_cmd'
    '12': '',  # ir_input'
    '13': '',  # recall_factory_settings'
    '14': '',  # inputname'
    '15': '',  # manufacturer'
    '16': '',  # modelname'
    '17': ''  # swversion'
}
# TODO:
# IMPORTANT
# * Continouosly update model based on what the reader thread sends up
# * Better reply handling than table?
# * Remove result validation or use it in PRIMARE_CMD dict
# * Better error handling
#       After suspend/resume, if volume up/down fails (or similar), try turning amp on
#
# LATER
# * v2: Implement as module(?), not class, for multiple writers/subscribers (singleton)
#       Seems like a factory would be better, so 'import primare_serial' then primare_serial.initComs()
#       which then creates the single Serial object.
# * v2: Add notification callback mechanism to notify users of changes on amp (dials or other SW)
#       http://bit.ly/WGRn0g
#       Better idea: websocket - http://forums.lantronix.com/showthread.php?p=3131
# * ...


class PrimareTalker():

    """
    Independent thread which does the communication with the Primare amplifier.

    Since the communication is done in an independent thread, Mopidy won't
    block other requests while doing time consuming work.
    """

    # Serial link config
    BAUDRATE = 4800
    BYTESIZE = 8
    PARITY = 'N'
    STOPBITS = 1

    # Timeout in seconds used for read/write operations.
    # If you set the timeout too low, the reads will never get complete
    # confirmations and calibration will decrease volume forever. If you set
    # the timeout too high, stuff takes more time. 0.8s seems like a good value
    # for Primare I22.
    TIMEOUT = 2.0

    # Number of volume levels the amplifier supports.
    # Primare amplifiers have 79 levels
    VOLUME_LEVELS = 79

    def __init__(self, unsolicited_cb, port, source, volume):
        self._unsolicited_cb = unsolicited_cb
        self._port = port
        self._source = source
        # Volume in range 0..VOLUME_LEVELS. :class:`None` before calibration.
        self._volume = int(volume) if volume else None

        self._alive = True
        self._mute_state = False
        self._device = None
        self._write_lock = threading.Lock()
        self._read_event = threading.Event()

        self._setup()

    # Private methods
    def _setup(self):
        logging.basicConfig(level=logging.DEBUG)
        self._open_connection()

        logger.debug('setup - starting thread')
        self.thread_read = threading.Thread(target=self._primare_reader)
        self.thread_read.setName('PrimareSerial')
        self.thread_read.start()

        self._set_device_to_known_state()
        self._print_device_info()

    def _open_connection(self):
        logger.info('Primare amplifier: Connecting through "%s"', self._port)
        self._device = serial.Serial(
            port=self._port,
            baudrate=self.BAUDRATE,
            bytesize=self.BYTESIZE,
            parity=self.PARITY,
            stopbits=self.STOPBITS,
            timeout=self.TIMEOUT)
        if self._device is None:
            raise exceptions.MixerError("Failed to start serial " +
                                        "connection to amplifier")

    def _set_device_to_known_state(self):
        logger.debug('_set_device_to_known_state')
        self.verbose_set(True)
        self.power_on()
        time.sleep(1)
        if self._source is not None:
            self.input_set(self._source)
        self.mute_set(False)
        if self._volume:
            self.volume_set(self._volume)
        else:
            self._volume = self._get_current_volume()

    def _print_device_info(self):
        logger.info("""Connected to:
            Manufacturer:  %s
            Model:         %s
            SW Version:    %s
            Current input: %s """,
                    self.manufacturer_get(),
                    self.modelname_get(),
                    self.swversion_get(),
                    self.inputname_current_get())

    def _get_current_volume(self):
        """Read current volume by doing volume_down and then
        volume_up and use reply from that to know the current volume
        The reply from the amplifier is in hex, so convert to decimal"""
        reply = self._send_command('volume_down')
        if reply:
            reply = self._send_command('volume_up')
            if reply:
                return int(reply, 16)
        return 0

    def _primare_reader(self):
        """Read data from the serial port

        Uses a modified version of pySerial's readline as EOL is '\x10\x03'
        Also replace any '\x10\x10' sequences with '\x10'.
        Returns the data received between the STX and DLE+ETX markers
        """
        # The reader will forever do readline, unless _send_command
        # takes the lock to send a command and get a reply

        logger.debug('_primare_reader - starting')

        while(self._alive):
            variable_char = ''
            data = ''
            #logger.debug('_primare_reader - still alive')

            # Read line from device.
            if not self._device.isOpen():
                self._device.open()

            eol = BYTE_DLE_ETX
            # Modified version of pySerial's readline
            leneol = len(eol)

            bytes_read = bytearray()

            while True:
                #logger.debug('_primare_reader - pre-read')
                c = self._device.read(1)
                #logger.debug('_primare_reader - post-read: %s', binascii.hexlify(c))
                if c:
                    bytes_read += c
                    if bytes_read[-leneol:] == eol:
                        break
                    else:
                        #logger.debug('_primare_reader - not-eol: %s', binascii.hexlify(bytes_read[-leneol:]))
                        pass

                else:
                    break
            # End of 'Modified version of pySerial's readline'

            if bytes_read:
                logger.debug('Read: "%s"', binascii.hexlify(bytes_read))
                byte_string = struct.unpack('c' * len(bytes_read), bytes_read)
                variable_char =  binascii.hexlify(''.join(byte_string[POS_REPLY_VAR]))
                byte_string = byte_string[POS_REPLY_DATA]

                # We need to replace double DLE (0x10) with single DLE
                for byte_pairs in zip(byte_string[0:None:2],
                                      byte_string[1:None:2]):
                    # Convert binary tuple to str to ascii
                    str_pairs = binascii.hexlify(''.join(byte_pairs))
                    if str_pairs == '1010':
                        data += '10'
                    else:
                        data += str_pairs
                # Very often we have an odd amount of data which not handled by
                # the zip above, manually append that one byte
                if len(byte_string) % 2 != 0:
                    data += binascii.hexlify(byte_string[-1])

                logger.debug('_primare_reader - index: "%s"', str.upper(variable_char))

                PRIMARE_REPLY[str.upper(variable_char)] = data
                self._read_event.set()
                self._read_event.clear()
                logger.debug('_primare_reader - post-event set')
                # TODO: Wait on event so that _send_command is done and then broadcast to subscribers
                # But, what then, to do when event received without a command sent? :-)
                # For now, just check if mute and volume has changed and update mixer with that
                # if PRIMARE_REPLY['03'] != '':
                #     self._unsolicited_cb('03', PRIMARE_REPLY['03'])
                #     PRIMARE_REPLY['03'] = ''
                # if PRIMARE_REPLY['09'] != '':
                #     self._unsolicited_cb('09', PRIMARE_REPLY['09'])
                #     PRIMARE_REPLY['09'] = ''
            else:
                logger.debug('Read(0): "%s" - len: %d',
                             bytes_read, len(bytes_read))

            #print "_primare_reader - readline, var: '%s' - data: '%s'" % (variable_char, data)

    def _send_command(self, variable, option=None):
        """Send the specified command to the amplifier

        :param variable: String key for the PRIMARE_CMD dict
        :type variable: string
        :param option: String value needed for some of the commands
        :type option: string
        :rtype: :class:`True` if success, :class:`False` if failure
        """
        reply_data = ''
        logger.debug('_send_command - pre lock - lock.locked: %s',
                     self._write_lock.locked())
        with self._write_lock:
            #logger.debug('_send_command - in lock')
            command = PRIMARE_CMD[variable][INDEX_CMD]
            data = PRIMARE_CMD[variable][INDEX_VARIABLE]
            reply = PRIMARE_CMD[variable][INDEX_REPLY]
            if option is not None:
                logger.debug('_send_command - replace YY with "%s"', option)
                data = data.replace('YY', option)
            #logger.debug(
            #    '_send_command - before write - cmd: "%s", ' +
            #    'data: "%s", option: "%s"',
            #    command, data, option)
            self._write(command, data)
            #logger.debug('_send_command - after write - data: %s', data)
            if PRIMARE_CMD[variable][INDEX_WAIT] == True:
                self._read_event.wait()
                reply_data = PRIMARE_REPLY[reply[0:2]]
                PRIMARE_REPLY[reply] = ''
            else:
                reply_data = reply
            logger.debug('_send_command - after event')
            self._read_event.set()
            self._read_event.clear()
        logger.debug('_send_command - post lock - reply_data: %s', reply_data)
        return reply_data

    def _write(self, cmd_type, data):
        """Write data to the serial port

        Any occurences of '\x10' must be replaced with '\x10\x10' and add
        the STX and DLE+ETX markers
        """
        # We need to replace single DLE (0x10) with double DLE to discern it
        data_safe = ''
        for index in range(0, len(data) - 1, 2):
            pair = data[index:index + 2]
            if pair == '10':
                data_safe += '1010'
            else:
                data_safe += pair
        # Convert ascii string to binary
        binary_variable = binascii.unhexlify(data_safe)
        logger.debug(
            '_write - cmd_type: "%s", data_safe: "%s"',
            cmd_type, data_safe)

        if cmd_type == 'W':
            binary_data = (BYTE_STX + BYTE_WRITE +
                           binary_variable + BYTE_DLE_ETX)
        else:
            binary_data = (BYTE_STX + BYTE_READ +
                           binary_variable + BYTE_DLE_ETX)
        # Write data to device.
        if not self._device.isOpen():
            self._device.open()
        self._device.write(binary_data)
        logger.debug('WriteHex(S): %s', binascii.hexlify(binary_data))


    # Public methods
    def stop_reader(self):
        self._alive = False
        self._send_command('verbose_toggle')
        self.thread_read.join()
        logger.info("Primare reader thread finished...exiting")

    def power_on(self):
        """Power on the Primare amplifier."""
        self._send_command('power_set', '01')

    def power_off(self):
        """Power off the Primare amplifier."""
        self._send_command('power_set', '00')

    def power_toggle(self):
        """Toggle the power to the Primare amplifier.

        :rtype: :class:True if amplifier turned on as result of toggle,
          :class:False otherwise
        """
        reply = self._send_command('power_toggle')
        if reply and reply == '01':
            return True
        else:
            return False

    def input_set(self, input_source):
        """Set the current input used by the Primare amplifier.

        :rtype: name of current input if success, empty string if failure
        """
        reply = self._send_command('input_set', '%02X' % int(input_source))
        if reply:
            return self.inputname_current_get()
        else:
            return ""

    def input_next(self):
        pass

    def input_prev(self):
        pass

    def volume_get(self):
        """
        Get volume level of the mixer on a linear scale from 0 to 100.

        Example values:

        0:
        Silent
        100:
        Maximum volume.
        :class:`None`:
        Volume is unknown.

        :rtype: int in range [0..100] or :class:`None`
        """
        logger.debug("LASSE - volume_get: %d", self._volume)
        return self._volume

    def volume_set(self, volume):
        """
        Set volume level of the amplifier.

        :param volume: Volume in the range [0..100]
        :type volume: int
        :rtype: :class:`True` if success, :class:`False` if failure
        """
        target_primare_volume = int(round(volume * self.VOLUME_LEVELS / 100.0))
        logger.debug("LASSE - target volume: %d", target_primare_volume)
        reply = self._send_command('volume_set',
                                   '%02X' % target_primare_volume)
        # There's a crazy bug where setting the volume to 65 and above will
        # generate a reply indicating a volume of 1 less!?
        # Hence the work-around
        if reply and (int(reply, 16) == target_primare_volume or
                      int(reply, 16) == target_primare_volume - 1):
            self._volume = volume
            logger.debug("LASSE - target volume SUCCESS, _volume: %d", self._volume)
            return True
        else:
            return False

    def volume_up(self):
        reply = self._send_command('volume_up')
        if reply:
            self._volume += 1
        return self._volume.get()

    def volume_down(self):
        reply = self._send_command('volume_down')
        if reply:
            self._volume -= 1
        return self._volume.get()

    def balance_adjust(self, adjustment):
        pass

    def balance_set(self, balance):
        pass

    def mute_toggle(self):
        reply = self._send_command('mute_toggle')
        if reply == '01':
            self._mute_state = True
            return True
        else:
            self._mute_state = False
            return False

    def mute_get(self):
        """
        Get mute state of the mixer.

        :rtype: :class:`True` if muted, :class:`False` if unmuted,
        :class:`None` if unknown.
        """
        return self._mute_state

    def mute_set(self, mute):
        """
        Mute or unmute the amplifier.

        :param mute: :class:`True` to mute, :class:`False` to unmute
        :type mute: bool
        :rtype: :class:`True` if success, :class:`False` if failure
        """
        mute_value = '01' if mute is True else '00'
        reply = self._send_command('mute_set', mute_value)
        if reply == '01':
            self._mute_state = True
            return True
        else:
            self._mute_state = False
            return False

    def dim_cycle(self):
        pass

    def dim_set(self, level):
        pass

    def verbose_toggle(self):
        pass

    def verbose_set(self, verbose):
        if verbose:
            self._send_command('verbose_set', '01')
        else:
            self._send_command('verbose_set', '00')

    def menu_toggle(self, verbose):
        pass

    def menu_set(self, menu):
        pass

    def remote_cmd(self, cmd):
        pass

    def ir_input_toggle(self):
        pass

    def ir_input_set(self, ir_input):
        pass

    def recall_factory_settings(self):
        pass

    def manufacturer_get(self):
        return binascii.unhexlify(
            self._send_command('manufacturer_get'))

    def modelname_get(self):
        return binascii.unhexlify(self._send_command('modelname_get'))

    def swversion_get(self):
        return binascii.unhexlify(self._send_command('swversion_get'))

    def inputname_current_get(self):
        return binascii.unhexlify(self._send_command('inputname_current_get'))

    def inputname_specific_get(self, input):
        if input >= 0 and input <= 7:
            return binascii.unhexlify(self._send_command('inputname_specific_get', '%02d' % input))
