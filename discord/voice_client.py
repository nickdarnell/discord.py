"""
The MIT License (MIT)

Copyright (c) 2015-present Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

import asyncio
import logging
import select
import socket
import struct
import threading
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union

from . import opus, utils
from .backoff import ExponentialBackoff
from .errors import ClientException, ConnectionClosed
from .gateway import *
from .player import AudioPlayer, AudioSource
from .sink import AudioReceiver, AudioSink
from .utils import MISSING
from .voice_state import VoiceConnectionState

if TYPE_CHECKING:
    from . import abc
    from .channel import StageChannel, VoiceChannel
    from .gateway import DiscordVoiceWebSocket
    from .client import Client
    from .guild import Guild
    from .opus import Decoder, Encoder
    from .state import ConnectionState
    from .user import ClientUser
    from .opus import Encoder, APPLICATION_CTL, BAND_CTL, SIGNAL_CTL
    from .channel import StageChannel, VoiceChannel
    from . import abc

    from .types.voice import (
        GuildVoiceState as GuildVoiceStatePayload,
        SupportedModes,
        VoiceServerUpdate as VoiceServerUpdatePayload,
    )
    from .user import ClientUser

    VocalGuildChannel = Union[VoiceChannel, StageChannel]


has_nacl: bool

try:
    import nacl.secret  # type: ignore
    import nacl.utils  # type: ignore

    has_nacl = True
except ImportError:
    has_nacl = False

__all__ = (
    'VoiceProtocol',
    'VoiceClient',
)


_log = logging.getLogger(__name__)


class VoiceProtocol:
    """A class that represents the Discord voice protocol.

    This is an abstract class. The library provides a concrete implementation
    under :class:`VoiceClient`.

    This class allows you to implement a protocol to allow for an external
    method of sending voice, such as Lavalink_ or a native library implementation.

    These classes are passed to :meth:`abc.Connectable.connect <VoiceChannel.connect>`.

    .. _Lavalink: https://github.com/freyacodes/Lavalink

    Parameters
    ------------
    client: :class:`Client`
        The client (or its subclasses) that started the connection request.
    channel: :class:`abc.Connectable`
        The voice channel that is being connected to.
    """

    def __init__(self, client: Client, channel: abc.Connectable) -> None:
        self.client: Client = client
        self.channel: abc.Connectable = channel

    async def on_voice_state_update(self, data: GuildVoiceStatePayload, /) -> None:
        """|coro|

        An abstract method that is called when the client's voice state
        has changed. This corresponds to ``VOICE_STATE_UPDATE``.

        Parameters
        ------------
        data: :class:`dict`
            The raw :ddocs:`voice state payload <resources/voice#voice-state-object>`.
        """
        raise NotImplementedError

    async def on_voice_server_update(self, data: VoiceServerUpdatePayload, /) -> None:
        """|coro|

        An abstract method that is called when initially connecting to voice.
        This corresponds to ``VOICE_SERVER_UPDATE``.

        Parameters
        ------------
        data: :class:`dict`
            The raw :ddocs:`voice server update payload <topics/gateway#voice-server-update>`.
        """
        raise NotImplementedError

    async def connect(self, *, timeout: float, reconnect: bool, self_deaf: bool = False, self_mute: bool = False) -> None:
        """|coro|

        An abstract method called when the client initiates the connection request.

        When a connection is requested initially, the library calls the constructor
        under ``__init__`` and then calls :meth:`connect`. If :meth:`connect` fails at
        some point then :meth:`disconnect` is called.

        Within this method, to start the voice connection flow it is recommended to
        use :meth:`Guild.change_voice_state` to start the flow. After which,
        :meth:`on_voice_server_update` and :meth:`on_voice_state_update` will be called.
        The order that these two are called is unspecified.

        Parameters
        ------------
        timeout: :class:`float`
            The timeout for the connection.
        reconnect: :class:`bool`
            Whether reconnection is expected.
        self_mute: :class:`bool`
            Indicates if the client should be self-muted.

            .. versionadded:: 2.0
        self_deaf: :class:`bool`
            Indicates if the client should be self-deafened.

            .. versionadded:: 2.0
        """
        raise NotImplementedError

    async def disconnect(self, *, force: bool) -> None:
        """|coro|

        An abstract method called when the client terminates the connection.

        See :meth:`cleanup`.

        Parameters
        ------------
        force: :class:`bool`
            Whether the disconnection was forced.
        """
        raise NotImplementedError

    def cleanup(self) -> None:
        """This method *must* be called to ensure proper clean-up during a disconnect.

        It is advisable to call this from within :meth:`disconnect` when you are
        completely done with the voice protocol instance.

        This method removes it from the internal state cache that keeps track of
        currently alive voice clients. Failure to clean-up will cause subsequent
        connections to report that it's still connected.
        """
        key_id, _ = self.channel._get_voice_client_key()
        self.client._connection._remove_voice_client(key_id)


class VoiceClient(VoiceProtocol):
    """Represents a Discord voice connection.

    You do not create these, you typically get them from
    e.g. :meth:`VoiceChannel.connect`.

    Warning
    --------
    In order to use PCM based AudioSources, you must have the opus library
    installed on your system and loaded through :func:`opus.load_opus`.
    Otherwise, your AudioSources must be opus encoded (e.g. using :class:`FFmpegOpusAudio`)
    or the library will not be able to transmit audio.

    Attributes
    -----------
    session_id: :class:`str`
        The voice connection session ID.
    token: :class:`str`
        The voice connection token.
    endpoint: :class:`str`
        The endpoint we are connecting to.
    channel: Union[:class:`VoiceChannel`, :class:`StageChannel`]
        The voice channel connected to.
    """

    channel: VocalGuildChannel

    def __init__(self, client: Client, channel: abc.Connectable) -> None:
        if not has_nacl:
            raise RuntimeError("PyNaCl library needed in order to use voice")

        super().__init__(client, channel)
        state = client._connection
        self.server_id: int = MISSING
        self.socket = MISSING
        self.loop: asyncio.AbstractEventLoop = state.loop
        self._state: ConnectionState = state

        self.sequence: int = 0
        self.timestamp: int = 0
        self._player: Optional[AudioPlayer] = None
        self._receiver: Optional[AudioReceiver] = None
        self.encoder: Encoder = MISSING
        self.decoders: Dict[int, Decoder] = {}
        self._lite_nonce: int = 0

        self._connection: VoiceConnectionState = self.create_connection_state()

    warn_nacl: bool = not has_nacl
    supported_modes: Tuple[SupportedModes, ...] = (
        'xsalsa20_poly1305_lite',
        'xsalsa20_poly1305_suffix',
        'xsalsa20_poly1305',
    )

    @property
    def guild(self) -> Guild:
        """:class:`Guild`: The guild we're connected to."""
        return self.channel.guild

    @property
    def user(self) -> ClientUser:
        """:class:`ClientUser`: The user connected to voice (i.e. ourselves)."""
        return self._state.user  # type: ignore

    @property
    def session_id(self) -> Optional[str]:
        return self._connection.session_id

    @property
    def token(self) -> Optional[str]:
        return self._connection.token

    @property
    def endpoint(self) -> Optional[str]:
        return self._connection.endpoint

    @property
    def ssrc(self) -> int:
        return self._connection.ssrc

    @property
    def mode(self) -> SupportedModes:
        return self._connection.mode

    @property
    def secret_key(self) -> List[int]:
        return self._connection.secret_key

    @property
    def ws(self) -> DiscordVoiceWebSocket:
        return self._connection.ws

    @property
    def timeout(self) -> float:
        return self._connection.timeout

    def checked_add(self, attr: str, value: int, limit: int) -> None:
        val = getattr(self, attr)
        if val + value > limit:
            setattr(self, attr, 0)
        else:
            setattr(self, attr, val + value)

    # connection related

    def create_connection_state(self) -> VoiceConnectionState:
        return VoiceConnectionState(self)

    async def on_voice_state_update(self, data: GuildVoiceStatePayload) -> None:
        await self._connection.voice_state_update(data)

    async def on_voice_server_update(self, data: VoiceServerUpdatePayload) -> None:
        await self._connection.voice_server_update(data)

        self.token = data['token']
        self.server_id = int(data['guild_id'])
        endpoint = data.get('endpoint')

        if endpoint is None or self.token is None:
            _log.warning(
                'Awaiting endpoint... This requires waiting. '
                'If timeout occurred considering raising the timeout and reconnecting.'
            )
            return

        self.endpoint, _, _ = endpoint.rpartition(':')
        if self.endpoint.startswith('wss://'):
            # Just in case, strip it off since we're going to add it later
            self.endpoint: str = self.endpoint[6:]

        # This gets set later
        self.endpoint_ip = MISSING

        self.socket: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setblocking(False)
        self._receiver = AudioReceiver(self)
        self._receiver.start()

        if not self._handshaking:
            # If we're not handshaking then we need to terminate our previous connection in the websocket
            await self.ws.close(4000)
            return

        self._voice_server_complete.set()

    async def voice_connect(self, self_deaf: bool = False, self_mute: bool = False) -> None:
        await self.channel.guild.change_voice_state(channel=self.channel, self_deaf=self_deaf, self_mute=self_mute)

    async def voice_disconnect(self) -> None:
        _log.info('The voice handshake is being terminated for Channel ID %s (Guild ID %s)', self.channel.id, self.guild.id)
        await self.channel.guild.change_voice_state(channel=None)

    def prepare_handshake(self) -> None:
        self._voice_state_complete.clear()
        self._voice_server_complete.clear()
        self._handshaking = True
        _log.info('Starting voice handshake... (connection attempt %d)', self._connections + 1)
        self._connections += 1

    def finish_handshake(self) -> None:
        _log.info('Voice handshake complete. Endpoint found %s', self.endpoint)
        self._handshaking = False
        self._voice_server_complete.clear()
        self._voice_state_complete.clear()

    async def connect_websocket(self) -> DiscordVoiceWebSocket:
        ws = await DiscordVoiceWebSocket.from_client(self)
        self._connected.clear()
        while ws.secret_key is None:
            await ws.poll_event()
        self._connected.set()
        return ws

    async def connect(self, *, reconnect: bool, timeout: float, self_deaf: bool = False, self_mute: bool = False) -> None:
        await self._connection.connect(
            reconnect=reconnect, timeout=timeout, self_deaf=self_deaf, self_mute=self_mute, resume=False
        )

    def wait_until_connected(self, timeout: Optional[float] = 30.0) -> bool:
        self._connection.wait(timeout)
        return self._connection.is_connected()

    @property
    def latency(self) -> float:
        """:class:`float`: Latency between a HEARTBEAT and a HEARTBEAT_ACK in seconds.

        This could be referred to as the Discord Voice WebSocket latency and is
        an analogue of user's voice latencies as seen in the Discord client.

        .. versionadded:: 1.4
        """
        ws = self._connection.ws
        return float("inf") if not ws else ws.latency

    @property
    def average_latency(self) -> float:
        """:class:`float`: Average of most recent 20 HEARTBEAT latencies in seconds.

        .. versionadded:: 1.4
        """
        ws = self._connection.ws
        return float("inf") if not ws else ws.average_latency

    async def disconnect(self, *, force: bool = False) -> None:
        """|coro|

        Disconnects this voice client from voice.
        """
        self.stop()
        await self._connection.disconnect(force=force)
        self.cleanup()
        self._receiver.stop()
        self._connected.clear()

    async def move_to(self, channel: Optional[abc.Snowflake], *, timeout: Optional[float] = 30.0) -> None:
        """|coro|

        Moves you to a different voice channel.

        Parameters
        -----------
        channel: Optional[:class:`abc.Snowflake`]
            The channel to move to. Must be a voice channel.
        timeout: Optional[:class:`float`]
            How long to wait for the move to complete.

            .. versionadded:: 2.4

        Raises
        -------
        asyncio.TimeoutError
            The move did not complete in time, but may still be ongoing.
        """
        await self._connection.move_to(channel, timeout)

    def is_connected(self) -> bool:
        """Indicates if the voice client is connected to voice."""
        return self._connection.is_connected()

    # audio related

    def _get_voice_packet(self, data):
        header = bytearray(12)

        # Formulate rtp header
        header[0] = 0x80
        header[1] = 0x78
        struct.pack_into('>H', header, 2, self.sequence)
        struct.pack_into('>I', header, 4, self.timestamp)
        struct.pack_into('>I', header, 8, self.ssrc)

        encrypt_packet = getattr(self, '_encrypt_' + self.mode)
        return encrypt_packet(header, data)

    def _encrypt_xsalsa20_poly1305(self, header: bytes, data) -> bytes:
        box = nacl.secret.SecretBox(bytes(self.secret_key))
        nonce = bytearray(24)
        nonce[:12] = header

        return header + box.encrypt(bytes(data), bytes(nonce)).ciphertext

    def _encrypt_xsalsa20_poly1305_suffix(self, header: bytes, data) -> bytes:
        box = nacl.secret.SecretBox(bytes(self.secret_key))
        nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)

        return header + box.encrypt(bytes(data), nonce).ciphertext + nonce

    def _encrypt_xsalsa20_poly1305_lite(self, header: bytes, data) -> bytes:
        box = nacl.secret.SecretBox(bytes(self.secret_key))
        nonce = bytearray(24)

        nonce[:4] = struct.pack('>I', self._lite_nonce)
        self.checked_add('_lite_nonce', 1, 4294967295)

        return header + box.encrypt(bytes(data), bytes(nonce)).ciphertext + nonce[:4]

    def play(
        self,
        source: AudioSource,
        *,
        after: Optional[Callable[[Optional[Exception]], Any]] = None,
        application: APPLICATION_CTL = 'audio',
        bitrate: int = 128,
        fec: bool = True,
        expected_packet_loss: float = 0.15,
        bandwidth: BAND_CTL = 'full',
        signal_type: SIGNAL_CTL = 'auto',
    ) -> None:
        """Plays an :class:`AudioSource`.

        The finalizer, ``after`` is called after the source has been exhausted
        or an error occurred.

        If an error happens while the audio player is running, the exception is
        caught and the audio player is then stopped.  If no after callback is
        passed, any caught exception will be logged using the library logger.

        Extra parameters may be passed to the internal opus encoder if a PCM based
        source is used.  Otherwise, they are ignored.

        .. versionchanged:: 2.0
            Instead of writing to ``sys.stderr``, the library's logger is used.

        .. versionchanged:: 2.4
            Added encoder parameters as keyword arguments.

        Parameters
        -----------
        source: :class:`AudioSource`
            The audio source we're reading from.
        after: Callable[[Optional[:class:`Exception`]], Any]
            The finalizer that is called after the stream is exhausted.
            This function must have a single parameter, ``error``, that
            denotes an optional exception that was raised during playing.
        application: :class:`str`
            Configures the encoder's intended application.  Can be one of:
            ``'audio'``, ``'voip'``, ``'lowdelay'``.
            Defaults to ``'audio'``.
        bitrate: :class:`int`
            Configures the bitrate in the encoder.  Can be between ``16`` and ``512``.
            Defaults to ``128``.
        fec: :class:`bool`
            Configures the encoder's use of inband forward error correction.
            Defaults to ``True``.
        expected_packet_loss: :class:`float`
            Configures the encoder's expected packet loss percentage.  Requires FEC.
            Defaults to ``0.15``.
        bandwidth: :class:`str`
            Configures the encoder's bandpass.  Can be one of:
            ``'narrow'``, ``'medium'``, ``'wide'``, ``'superwide'``, ``'full'``.
            Defaults to ``'full'``.
        signal_type: :class:`str`
            Configures the type of signal being encoded.  Can be one of:
            ``'auto'``, ``'voice'``, ``'music'``.
            Defaults to ``'auto'``.

        Raises
        -------
        ClientException
            Already playing audio or not connected.
        TypeError
            Source is not a :class:`AudioSource` or after is not a callable.
        OpusNotLoaded
            Source is not opus encoded and opus is not loaded.
        ValueError
            An improper value was passed as an encoder parameter.
        """

        if not self.is_connected():
            raise ClientException('Not connected to voice.')

        if self.is_playing():
            raise ClientException('Already playing audio.')

        if not isinstance(source, AudioSource):
            raise TypeError(f'source must be an AudioSource not {source.__class__.__name__}')

        if not source.is_opus():
            self.encoder = opus.Encoder(
                application=application,
                bitrate=bitrate,
                fec=fec,
                expected_packet_loss=expected_packet_loss,
                bandwidth=bandwidth,
                signal_type=signal_type,
            )

        self._player = AudioPlayer(source, self, after=after)
        self._player.start()

    def is_playing(self) -> bool:
        """Indicates if we're currently playing audio."""
        return self._player is not None and self._player.is_playing()

    def is_paused(self) -> bool:
        """Indicates if we're playing audio, but if we're paused."""
        return self._player is not None and self._player.is_paused()

    def stop(self) -> None:
        """Stops playing audio."""
        if self._player:
            self._player.stop()
            self._player = None

    def pause(self) -> None:
        """Pauses the audio playing."""
        if self._player:
            self._player.pause()

    def resume(self) -> None:
        """Resumes the audio playing."""
        if self._player:
            self._player.resume()

    def listen(
        self,
        sink: AudioSink,
        *,
        decode: bool = True,
        supress_warning: bool = False,
        after: Optional[Callable[..., Awaitable[Any]]] = None,
        **kwargs,
    ) -> None:
        """Receives audio into an :class:`AudioSink`

        IMPORTANT: If you call this function, the running section of your code should be
        contained within an `if __name__ == "__main__"` statement to avoid conflicts with
        multiprocessing that result in the asyncio event loop dying.

        The finalizer, ``after`` is called after listening has stopped or
        an error has occurred.

        If an error happens while the audio receiver is running, the exception is
        caught and the audio receiver is then stopped.  If no after callback is
        passed, any caught exception will be logged using the library logger.

        If this function is called multiple times, it is recommended to use
        wait_for_listen_ready before making the next call to avoid errors.

        Parameters
        -----------
        sink: :class:`AudioSink`
            The audio sink we're passing audio to.
        decode: :class:`bool`
            Whether to decode data received from discord.
        supress_warning: :class:`bool`
            Whether to supress the warning raised when listen is run unsafely.
        after: Callable[..., Awaitable[Any]]
            The finalizer that is called after the receiver stops. This function
            must be a coroutine function. This function must have at least two
            parameters, ``sink`` and ``error``, that denote, respectfully, the
            sink passed to this function and an optional exception that was
            raised during playing. The function can have additional arguments
            that match the keyword arguments passed to this function.

        Raises
        -------
        ClientException
            Already listening, not connected, or must initialize audio processing pool before listening.
        TypeError
            sink is not an :class:`AudioSink` or after is not a callable.
        OpusNotLoaded
            Opus, required to decode audio, is not loaded.
        """
        if not self.is_connected():
            raise ClientException('Not connected to voice.')

        if self.is_listen_receiving():
            raise ClientException('Listening is already active.')

        if not isinstance(sink, AudioSink):
            raise TypeError(f"sink must be an AudioSink not {sink.__class__.__name__}")

        if not self.is_audio_process_pool_initialized():
            raise ClientException("Must initialize audio processing pool before listening.")

        if not supress_warning and self.is_listen_cleaning():
            _log.warning(
                "Cleanup is still in progress for the last call to listen and so errors may occur. "
                "It is recommended to use wait_for_listen_ready before calling listen unless you "
                "know what you're doing."
            )

        if decode:
            # Check that opus is loaded and throw error else
            opus.Decoder.get_opus_version()

        self._receiver.start_listening(sink, decode=decode, after=after, after_kwargs=kwargs)

    def init_audio_processing_pool(self, max_processes: int, *, wait_timeout: Optional[float] = 3) -> None:
        """Initialize audio processing pool. This function should only be called once from any one
        voice client object.

        Parameters
        ----------
        max_processes: :class:`int`
            The audio processing pool will distribute audio processing across
            this number of processes.
        wait_timeout: Optional[:class:`int`]
            A process will automatically finish when it has not received any audio
            after this amount of time. Default is 3. None means it will never finish
            via timeout.

        Raises
        ------
        RuntimeError
            Audio processing pool is already initialized
        ValueError
            max_processes or wait_timeout must be greater than 0
        """
        self._state.init_audio_processing_pool(max_processes, wait_timeout=wait_timeout)

    def is_audio_process_pool_initialized(self) -> bool:
        """Indicates if the audio process pool is active"""
        return self._state.is_audio_process_pool_initialized()

    def is_listening(self) -> bool:
        """Indicates if the client is currently listening and processing audio."""
        return self._receiver is not None and self._receiver.is_listening()

    def is_listening_paused(self) -> bool:
        """Indicate if the client is currently listening, but paused (not processing audio)."""
        return self._receiver is not None and self._receiver.is_paused()

    def is_listen_receiving(self) -> bool:
        """Indicates whether listening is active, regardless of the pause state."""
        return self._receiver is not None and not self._receiver.is_on_standby()

    def is_listen_cleaning(self) -> bool:
        """Check if the receiver is cleaning up."""
        return self._receiver is not None and self._receiver.is_cleaning()

    def stop_listening(self) -> None:
        """Stops listening"""
        if self._receiver:
            self._receiver.stop_listening()

    def pause_listening(self) -> None:
        """Pauses listening"""
        if self._receiver:
            self._receiver.pause()

    def resume_listening(self) -> None:
        """Resumes listening"""
        if self._receiver:
            self._receiver.resume()

    async def wait_for_listen_ready(self) -> None:
        """Wait till it's safe to make a call to listen.
        Basically waits for is_listen_receiving and is_listen_cleaning to be false.
        """
        if self._receiver is None:
            return
        await self._receiver.wait_for_standby()
        await self._receiver.wait_for_clean()

    @property
    def source(self) -> Optional[AudioSource]:
        """Optional[:class:`AudioSource`]: The audio source being played, if playing.

        This property can also be used to change the audio source currently being played.
        """
        return self._player.source if self._player else None

    @source.setter
    def source(self, value: AudioSource) -> None:
        if not isinstance(value, AudioSource):
            raise TypeError(f'expected AudioSource not {value.__class__.__name__}.')

        if self._player is None:
            raise ValueError('Not playing anything.')

        self._player.set_source(value)

    def send_audio_packet(self, data: bytes, *, encode: bool = True) -> None:
        """Sends an audio packet composed of the data.

        You must be connected to play audio.

        Parameters
        ----------
        data: :class:`bytes`
            The :term:`py:bytes-like object` denoting PCM or Opus voice data.
        encode: :class:`bool`
            Indicates if ``data`` should be encoded into Opus.

        Raises
        -------
        ClientException
            You are not connected.
        opus.OpusError
            Encoding the data failed.
        """

        self.checked_add('sequence', 1, 65535)
        if encode:
            encoded_data = self.encoder.encode(data, self.encoder.SAMPLES_PER_FRAME)
        else:
            encoded_data = data
        packet = self._get_voice_packet(encoded_data)
        try:
            self._connection.send_packet(packet)
        except OSError:
            _log.info('A packet has been dropped (seq: %s, timestamp: %s)', self.sequence, self.timestamp)

        self.checked_add('timestamp', opus.Encoder.SAMPLES_PER_FRAME, 4294967295)

    def recv_audio(self, *, dump: bool = False) -> Optional[bytes]:
        """Attempts to receive raw audio and returns it, otherwise nothing.

        You must be connected to receive audio.

        Raises any error thrown by the connection socket.

        Parameters
        ----------
        dump: :class:`bool`
            Will not return audio packet if true

        Returns
        -------
        Optional[bytes]
            If audio was received then it's returned.
        """
        ready, _, err = select.select([self.socket], [], [self.socket], 0.01)
        if err:
            _log.error(f"Socket error: {err[0]}")
            return
        if not ready or not self._connected.is_set():
            return

        data = self.socket.recv(4096)
        if dump:
            return
        return data
