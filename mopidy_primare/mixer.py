"""Mixer that controls volume using a Primare amplifier."""

from __future__ import unicode_literals

from twisted.internet.defer import inlineCallbacks
from twisted.protocols.basic import LineReceiver

from autobahn.twisted.wamp import ApplicationSession

from mopidy import mixer

import logging
import primare_serial
import pykka

logger = logging.getLogger(__name__)


class PrimareMixer(pykka.ThreadingActor, mixer.Mixer):

    name = 'primare'

    def __init__(self, config):
        super(PrimareMixer, self).__init__(config)

        self.port = config['primare']['port']
        self.source = config['primare']['source'] or None
        self.volume = config['primare']['volume'] or None

        self._primare = None

    def on_start(self):
        self._connect_primare()

    def on_stop(self):
        self._primare.stop_reader()

    def get_volume(self):
        """
        Get volume level of the mixer on a linear scale from 0 to 100.

        Example values:

        0:
        Minimum volume, usually silent.
        100:
        Maximum volume.
        :class:`None`:
        Volume is unknown.

        :rtype: int in range [0..100] or :class:`None`
        """
        return self._primare.volume_get()

    def set_volume(self, volume):
        """
        Set volume level of the mixer.

        :param volume: Volume in the range [0..100]
        :type volume: int
        :rtype: :class:`True` if success, :class:`False` if failure
        """
        success = self._primare.volume_set(volume)
        if success:
            self.trigger_volume_changed(volume)
        return success

    def get_mute(self):
        """
        Get mute state of the mixer.

        :rtype: :class:`True` if muted, :class:`False` if unmuted,
        :class:`None` if unknown.
        """
        return self._primare.mute_get()

    def set_mute(self, mute):
        """
        Mute or unmute the mixer.

        :param mute: :class:`True` to mute, :class:`False` to unmute
        :type mute: bool
        :rtype: :class:`True` if success, :class:`False` if failure
        """
        success = self._primare.mute_set(mute)
        if success:
            self.trigger_mute_changed(mute)
        return success

    def unsolicited_updates(self, variable, data):
        if variable == '03':
            logger.info('Primare mixer: Unsolicited VOLUME - data: %s', data)
            pass
        if variable == '09':
            logger.info('Primare mixer: Unsolicited MUTE - data: %s', data)
            pass

    def _connect_primare(self):
        logger.info('Primare mixer: Connecting through "%s", using input: %s',
                    self.port,
                    self.source if self.source is not None else "<DEFAULT>")
        self._primare = primare_serial.PrimareTalker(
            unsolicited_cb=self.actor_ref.proxy().unsolicited_updates,
            port=self.port, source=self.source, volume=self.volume
        )
