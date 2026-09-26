import argparse
import asyncio
from amaranth import *
from amaranth.lib import data, wiring, stream, io, cdc
from amaranth.lib.wiring import Out, In

from glasgow.support import logging
from glasgow.support.logging import dump_hex
from glasgow.gateware.stream import AsyncQueue
from glasgow.gateware import cobs
from glasgow.abstract import AbstractAssembly, GlasgowPin
from glasgow.applet import GlasgowAppletError, GlasgowAppletV2


__all__ = ["TDMComponent", "TDMApplet"]


class TDMFrontend(wiring.Component):
    '''
    Shifts frame_bits bits in/out, keeps alignment with fsync.
    If fsync is received at an unexpected time, raises an error and discards data to keep correct alignment.
    '''
    def __init__(self, ports, frame_bytes):
        self._ports = ports

        self._frame_bytes = frame_bytes 

        super().__init__({
            "rx_stream": Out(stream.Signature(8)),
            "overflow": Out(1), # FS is not received when expected
            "underflow": Out(1), # FS is received earlier than expected
        })

    def elaborate(self, platform):
        m = Module()

        m.submodules.bclk_buffer   = bclk_buffer  = io.Buffer("i", self._ports.bclk)
        m.submodules.fsync_buffer  = fsync_buffer = io.Buffer("i", self._ports.fsync)
        # m.submodules.tx_buffer     = tx_buffer    = io.Buffer("o", self._ports.tx)
        m.submodules.rx_buffer     = rx_buffer    = io.Buffer("i", self._ports.rx)

        if platform is not None:
            # Works on iCE40HX8K.
            platform.add_clock_constraint(bclk_buffer.i, 100e6)

        # Shifting logic works directly on incoming bclk clock
        m.domains.bclk = cd_bclk = ClockDomain(async_reset=True, local=True)
        m.d.comb += cd_bclk.clk.eq(bclk_buffer.i)

        m.submodules.fifo = fifo = AsyncQueue.shaped_like(self.rx_stream,
            depth=4, # CDC only, no buffering
            i_domain="bclk",
            o_domain="sync"
        )
        wiring.connect(m, wiring.flipped(self.rx_stream), fifo.o)

        shift = Signal(1)
        shift_done = Signal(1)
        with m.If(shift):
            rx_shreg = Signal(8)
            m.d.bclk += rx_shreg.eq(Cat(rx_buffer.i, rx_shreg))
            m.d.comb += fifo.i.p.eq(Cat(rx_buffer.i, rx_shreg))

            # TODO Maybe these could be combined into one signal (use module 8 when byte is full)
            bytes_shifted = Signal(range(self._frame_bytes))
            shift_count = Signal(range(8))
            with m.If(shift_count == 8 - 1):
                m.d.comb += fifo.i.valid.eq(1)
                m.d.bclk += shift_count.eq(0)
                with m.If(bytes_shifted == self._frame_bytes - 1):
                    m.d.bclk += bytes_shifted.eq(0)
                    m.d.comb += shift_done.eq(1)
                with m.Else():
                    m.d.bclk += bytes_shifted.eq(bytes_shifted + 1)

            with m.Else():
                m.d.bclk += shift_count.eq(shift_count + 1)

        with m.FSM(domain='bclk'):
            with m.State("Wait-fsync"):
                with m.If(fsync_buffer.i):
                    # shift first bit
                    m.d.comb += shift.eq(1)
                    m.next = "Shift-Frame"
                pass
            with m.State("Shift-Frame"):
                m.d.comb += shift.eq(1)
                with m.If(shift_done):
                    m.next = "Wait-fsync"
                # TODO detect wrong fsync timing
                pass
            with m.State("Fsync-early"):
                # Fsync received too early, send zeros and wait for next fsync
                # TODO how do I best report this?
                # A flag in a register?
                # An extra byte with each frame with status bits?
                pass
            with m.State("Fsync-late"):
                # Fsync received late
                pass


        return m


class TDMComponent(wiring.Component):
    o_stream: Out(stream.Signature(8))

    def __init__(self, ports, frame_bytes):
        self._ports = ports
        self._frame_bytes = frame_bytes

        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.frontend = frontend = TDMFrontend(self._ports, self._frame_bytes)
        wiring.connect(m, wiring.flipped(self.o_stream), frontend.rx_stream)

        return m


class TDMInterface:
    def __init__(self, logger: logging.Logger, assembly: AbstractAssembly, *,
                 bclk: GlasgowPin, fsync: GlasgowPin, rx: GlasgowPin,
                 frame_bytes):
        self._logger = logger
        self._level  = logging.DEBUG if self._logger.name == __name__ else logging.TRACE

        ports = assembly.add_port_group(bclk=bclk, fsync=fsync, rx=rx)
        component = assembly.add_submodule(TDMComponent(ports, frame_bytes))
        self._pipe = assembly.add_in_pipe(component.o_stream)

    def _log(self, message, *args):
        self._logger.log(self._level, "SPI analyzer: " + message, *args)


class TDMApplet(GlasgowAppletV2):
    logger = logging.getLogger(__name__)
    help = "Generate or receive audio via TDM/I2S"
    description = """
    """
    # Not tested on anything else
    required_revision = "C0"

    @classmethod
    def add_build_arguments(cls, parser, access):
        access.add_voltage_argument(parser)
        access.add_pins_argument(parser, "bclk",  required=True, default=True)
        access.add_pins_argument(parser, "fsync", required=True, default=True)
        access.add_pins_argument(parser, "rx",    required=True, default=True)
        # might support multiple data lines later
        # TODO implement tx later
        # access.add_pins_argument(parser, "tx",    required=True, default=True)
        #access.add_pins_argument(parser, "tx",    required=True, default=True, width=range(1, 8))

        parser.add_argument(
            "--frame-bytes", type=int, default=4,
            help="bytes in a frame, bclk cycles per fsync divided by 8, e.g. 16 bit x 2 channels = 4 bytes")

    def build(self, args):
        with self.assembly.add_applet(self):
            self.assembly.use_voltage(args.voltage)
            #self.assembly.use_pulls({args.cs: "high"})
            self.tdm_iface = TDMInterface(self.logger, self.assembly,
                bclk=args.bclk, fsync=args.fsync, rx=args.rx,
                frame_bytes=args.frame_bytes
            )

    @classmethod
    def add_run_arguments(cls, parser):
        parser.add_argument(
            "-l", "--loop", action='store_true',
            help="loop the txfile until the command is aborted")

        # parser.add_argument(
        #     "--txfile", type=argparse.FileType('rb'),
        #     help="raw audio file to transmit")

        parser.add_argument(
            "--rxfile", type=argparse.FileType('wb'),
            help="raw audio file to record to")

    async def run(self, args):
        async def write_task():
            if args.txfile:
                pcm_data = args.txfile.read()
                bytes_written = 0
                while True:
                    bytes_written += len(pcm_data)
                    await self.tdm_iface._pipe.send(pcm_data)
                    # Somehow if I don't call flush here nothing happens even though the writes get submitted
                    # I think it would make sense if I would only have to call flush once in the very end
                    await self.tdm_iface._pipe.flush(wait=False) 
                    if not args.loop:
                        break
                await self.tdm_iface.flush(wait=False)
                return bytes_written

        async def read_task(write_future):
            if args.rxfile:
                bytes_read = 0
                while not write_future.done() or bytes_read < write_future.result():

                    #TODO Can this get stuck waiting for one more byte even though the condition is satisfied somehow?
                    # flush=False has to be set, otherwise it can deadlock with the write task I think
                    d = bytes(await self.tdm_iface._pipe.recv()) 
                    bytes_read += len(d)
                    args.rxfile.write(d)

        write_future = asyncio.create_task(write_task())
        read_future = asyncio.create_task(read_task(write_future))
        await asyncio.gather(write_future, read_future)

    @classmethod
    def tests(cls):
        from . import test
        return test.TDMAppletTestCase
