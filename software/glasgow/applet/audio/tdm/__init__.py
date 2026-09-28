import argparse
import asyncio
from amaranth import *
from amaranth.lib import data, wiring, stream, io, cdc
from amaranth.lib.wiring import Out, In

from glasgow.support import logging
from glasgow.gateware.stream import AsyncQueue, Queue
from glasgow.gateware import pll
from glasgow.abstract import AbstractAssembly, GlasgowPin
from glasgow.applet import GlasgowAppletError, GlasgowAppletV2

# Next steps:
# Test with external signal and higher frequency
# Figure out PLL
# Implement tx
# stream.PacketQueue looks useful to assure frames are always complete
# what does IOStreamer do? Maybe its useful for correct sampling on rising/falling clock edge
# Or maybe I need a falling edge clock domain for one of TX / RX

'''

TDM bit clock has a driving edge and sampling edge.

From iMXRT1060 ref manual:
Bit Clock Polarity
Configures the polarity of the bit clock.
0b - Bit clock is active high with drive outputs on rising edge and sample inputs on falling edge.
1b - Bit clock is active low with drive outputs on falling edge and sample inputs on rising edge.

Default is drive on rising, sample on falling

Does the FPGA automatically do it like this?
It looks like it both samples and drives on rising? How does that work?

So the RX clock should be inverted

FSYNC must be asserted slightly before BCLK rising maybe? Texas Instr datasheets suggest that
NXP datasheet shows FS asserted slightly after BCLK rise

So TX must react to FS immediately
TX: First bit is asseted immediately when FS is received, after that at every bclk rising edge


The FPGA has to handle streaming in / out the data, the frame structure can be handled in software

We transmit LSB first

'''


__all__ = ["TDMComponent", "TDMApplet"]

class TDMClockGen(wiring.Component):
    '''
    Generates bclk and fsync
    '''
    fsync: Out(1)
    bclk: Out(1)

    def __init__(self, bclk_period, sys_clk_period, frame_bytes):
        super().__init__()
        self._bclk_period = bclk_period
        self._sys_clk_period = sys_clk_period
        self._frame_bytes = frame_bytes

    def elaborate(self, platform):
        m = Module()

        # PLL seems tricky, lets see later
        # plan = pll.ClockPlan(self._sys_clk_period)
        # m.domains.bclk = cd_bclk = plan.add_domain(pll.Channel(period=self._bclk_period))
        # m.submodules.pll = plan.create(platform)
        # TODO min PLL frequency is 16MHz, need a divisor for lower frequencies

        # m.d.comb += self.bclk.eq(cd_bclk.clk)

        # fclk_counter = Signal(range(self._frame_bytes * 8))
        # with m.If(fclk_counter == self._frame_bytes * 8 - 1):
        #     m.d.comb += self.fsync.eq(1)
        #     m.d.bclk += fclk_counter.eq(0)
        # with m.Else():
        #     m.d.bclk += fclk_counter.eq(fclk_counter + 1)

        bclk_counter = Signal(range(64))
        m.d.sync += bclk_counter.eq(bclk_counter + 1)
        m.d.comb += self.bclk.eq(bclk_counter[-1])

        fclk_counter = Signal(range(self._frame_bytes * 8))

        with m.If(bclk_counter == 0):
            with m.If(fclk_counter == self._frame_bytes * 8 - 1):
                m.d.comb += self.fsync.eq(1)
                m.d.sync += fclk_counter.eq(0)
            with m.Else():
                m.d.sync += fclk_counter.eq(fclk_counter + 1)
        m.d.comb += self.fsync.eq(fclk_counter == self._frame_bytes * 8 - 1)
        # TODO check fsync is asserted on correct bclk edge


        return m


class TDMTransmitter(wiring.Component):
    bclk: In(1)
    fsync: In(1)
    tx: Out(1)

    tx_stream: In(stream.Signature(8))

    def __init__(self, frame_bytes):
        self._frame_bytes = frame_bytes 
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        # Shifting logic works directly on incoming bclk clock
        m.domains.bclk = cd_bclk = ClockDomain(async_reset=True, local=True)
        m.d.comb += cd_bclk.clk.eq(self.bclk)

        m.submodules.tx_fifo = tx_fifo = AsyncQueue.shaped_like(self.tx_stream,
            depth=4, # CDC only, no buffering
            i_domain="sync",
            o_domain="bclk"
        )
        wiring.connect(m, wiring.flipped(self.tx_stream), tx_fifo.i)

        # Buffer one frame so we can't run out of data during a frame
        m.submodules.tx_frame_queue = tx_frame_queue = DomainRenamer("bclk")(
            Queue.shaped_like(self.tx_stream, depth=self._frame_bytes)
        )
        wiring.connect(m, tx_fifo.o, tx_frame_queue.i)

        tx_ready = Signal(1)
        m.d.comb += tx_ready.eq(tx_frame_queue.level >= self._frame_bytes)

        fsync_rising = Signal(1)
        last_fsync = Signal(1)
        m.d.bclk += last_fsync.eq(self.fsync)
        m.d.comb += fsync_rising.eq(self.fsync & ~last_fsync)

        shift = Signal(1)
        shift_done = Signal(1)
        tx_shreg = Signal(8)

        shift_delay = Signal(1)
        m.d.bclk += shift_delay.eq(shift)
        with m.If(shift | shift_delay):
            m.d.comb += self.tx.eq(tx_shreg[0])

        with m.If(shift):
            m.d.bclk += tx_shreg.eq(Cat(tx_shreg[1:], 0))

            # TODO Maybe these could be combined into one signal (use modulo 8 for when byte is full)
            bytes_shifted = Signal(range(self._frame_bytes))
            shift_count = Signal(range(8))
            with m.If(shift_count == 8 - 1):
                m.d.bclk += shift_count.eq(0)
                with m.If(bytes_shifted == self._frame_bytes - 1):
                    m.d.bclk += bytes_shifted.eq(0)
                    m.d.comb += shift_done.eq(1)
                with m.Else():
                    m.d.bclk += bytes_shifted.eq(bytes_shifted + 1)
            with m.Else():
                m.d.bclk += shift_count.eq(shift_count + 1)

            with m.If(shift_count == 0):
                m.d.comb += tx_frame_queue.o.ready.eq(1)
                m.d.bclk += tx_shreg.eq(tx_frame_queue.o.payload)
        with m.Else():
            # Preload shreg without consuming the fifo
            m.d.bclk += tx_shreg.eq(tx_frame_queue.o.payload)



        with m.FSM(domain='bclk'):
            with m.State("Wait-fsync"):
                with m.If(fsync_rising & tx_ready):
                    # shift first bit immediately
                    # TODO shreg has to be aleady filled, how do I do that?
                    m.d.comb += shift.eq(1)
                    m.next = "Shift-Frame"
                pass
            with m.State("Shift-Frame"):
                m.d.comb += shift.eq(1)
                with m.If(shift_done):
                    m.next = "Wait-fsync"
                pass


        return m

class TDMReceiver(wiring.Component):
    bclk: In(1)
    fsync: In(1)
    rx: In(1)

    rx_stream: Out(stream.Signature(8))

    def __init__(self, frame_bytes):
        self._frame_bytes = frame_bytes 
        super().__init__()

    def elaborate(self, platform):
        m = Module()


        # Shifting logic works directly on incoming bclk clock
        m.domains.rx_bclk = cd_bclk = ClockDomain(async_reset=True, local=True)
        # invert incoming bclk so that we sample on the falling clock edge
        m.d.comb += cd_bclk.clk.eq(~self.bclk)

        # Maybe I can move the CDC fifo out of this module, then the whole module uses the same clock domain
        # TODO if the fifo overflows we are currenly loosing frame sync
        m.submodules.rx_fifo = rx_fifo = AsyncQueue.shaped_like(self.rx_stream,
            depth=4, # CDC only, no buffering
            i_domain="rx_bclk",
            o_domain="sync"
        )
        wiring.connect(m, wiring.flipped(self.rx_stream), rx_fifo.o)


        fsync_rising = Signal(1)
        last_fsync = Signal(1)
        m.d.rx_bclk += last_fsync.eq(self.fsync)
        m.d.comb += fsync_rising.eq(self.fsync & ~last_fsync)

        shift = Signal(1)
        shift_done = Signal(1)
        with m.If(shift):
            rx_shreg = Signal(8)
            m.d.rx_bclk += rx_shreg.eq(Cat(self.rx, rx_shreg))
            m.d.comb += rx_fifo.i.p.eq(Cat(self.rx, rx_shreg))

            # TODO Maybe these could be combined into one signal (use modulo 8 for when byte is full)
            bytes_shifted = Signal(range(self._frame_bytes))
            shift_count = Signal(range(8))
            with m.If(shift_count == 8 - 1):
                m.d.comb += rx_fifo.i.valid.eq(1)
                m.d.rx_bclk += shift_count.eq(0)
                with m.If(bytes_shifted == self._frame_bytes - 1):
                    m.d.rx_bclk += bytes_shifted.eq(0)
                    m.d.comb += shift_done.eq(1)
                with m.Else():
                    m.d.rx_bclk += bytes_shifted.eq(bytes_shifted + 1)
            with m.Else():
                m.d.rx_bclk += shift_count.eq(shift_count + 1)


        with m.FSM(domain='rx_bclk'):
            with m.State("Wait-fsync"):
                with m.If(fsync_rising):
                    # shift first bit immediately
                    m.d.comb += shift.eq(1)
                    m.next = "Shift-Frame"
                pass
            with m.State("Shift-Frame"):
                m.d.comb += shift.eq(1)
                with m.If(shift_done):
                    with m.If(fsync_rising):
                        # If fsync is already asserted again, stay shifting
                        m.next = "Shift-Frame"
                    with m.Else():
                        m.next = "Wait-fsync"
                pass

        return m


class TDMComponent(wiring.Component):
    o_stream: Out(stream.Signature(8))
    i_stream: In(stream.Signature(8))

    def __init__(self, ports, frame_bytes, bclk_generator_period, sys_clk_period):
        self._ports = ports
        self._frame_bytes = frame_bytes
        self._bclk_generator_period = bclk_generator_period
        self._sys_clk_period = sys_clk_period

        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.receiver = receiver = TDMReceiver(self._frame_bytes)
        wiring.connect(m, wiring.flipped(self.o_stream), receiver.rx_stream)

        m.submodules.transmitter = transmitter = TDMTransmitter(self._frame_bytes)
        wiring.connect(m, wiring.flipped(self.i_stream), transmitter.tx_stream)

        if self._bclk_generator_period is not None:
            m.submodules.clockgen = clockgen = TDMClockGen(self._bclk_generator_period, self._sys_clk_period, self._frame_bytes)
            m.submodules.bclk_buffer   = bclk_buffer  = io.Buffer("o", self._ports.bclk)
            m.submodules.fsync_buffer  = fsync_buffer = io.Buffer("o", self._ports.fsync)
            m.d.comb += [
                bclk_buffer.o.eq(clockgen.bclk),
                fsync_buffer.o.eq(clockgen.fsync),
            ]
            m.d.comb += [
                receiver.bclk.eq(clockgen.bclk),
                receiver.fsync.eq(clockgen.fsync),
                transmitter.bclk.eq(clockgen.bclk),
                transmitter.fsync.eq(clockgen.fsync),
            ]
        else:
            m.submodules.bclk_buffer   = bclk_buffer  = io.Buffer("i", self._ports.bclk)
            m.submodules.fsync_buffer  = fsync_buffer = io.Buffer("i", self._ports.fsync)
            m.d.comb += [
                receiver.bclk.eq(bclk_buffer.i),
                receiver.fsync.eq(fsync_buffer.i),
                transmitter.bclk.eq(bclk_buffer.i),
                transmitter.fsync.eq(fsync_buffer.i),
            ]
            if platform is not None:
                # Works on iCE40HX8K.
                # TODO what does this do?
                platform.add_clock_constraint(bclk_buffer.i, 100e6)

        m.submodules.rx_buffer     = rx_buffer    = io.Buffer("i", self._ports.rx)
        m.submodules.tx_buffer     = tx_buffer    = io.Buffer("o", self._ports.tx)
        m.d.comb += [
            receiver.rx.eq(rx_buffer.i),
            # tx_buffer.o.eq(receiver.tx),
        ]

        return m


class TDMInterface:
    def __init__(self, logger: logging.Logger, assembly: AbstractAssembly, *,
                 bclk: GlasgowPin, fsync: GlasgowPin, rx: GlasgowPin, tx: GlasgowPin,
                 frame_bytes, bclk_generator_frequency: float | None):
        self._logger = logger
        self._level  = logging.DEBUG if self._logger.name == __name__ else logging.TRACE
        self._frame_bytes = frame_bytes

        ports = assembly.add_port_group(bclk=bclk, fsync=fsync, rx=rx, tx=tx)
        bclk_period = 1/bclk_generator_frequency if bclk_generator_frequency is not None else None
        component = assembly.add_submodule(TDMComponent(ports, frame_bytes, bclk_period, assembly.sys_clk_period))
        self._pipe = assembly.add_inout_pipe(component.o_stream, component.i_stream)

    def _log(self, message, *args):
        self._logger.log(self._level, "TDM: " + message, *args)

    async def write_frame(self, data):
        if len(data) != self._frame_bytes:
            raise ValueError("data has to be one frame long")
        await self._pipe.send(data)

    async def read_frame(self):
        return (await self._pipe.recv(self._frame_bytes)).tobytes()

    # Maybe have a play/rec/playrec interface like the sounddevice library


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
        access.add_pins_argument(parser, "tx",    required=True, default=True)
        # might support multiple data lines later
        #access.add_pins_argument(parser, "tx",    required=True, default=True, width=range(1, 8))

        # TODO maybe this could be a setup argument set by register
        # TODO might be easier to specify bitdepth, channels
        parser.add_argument(
            "--frame-bytes", type=int, default=4,
            help="bytes in a frame, bclk cycles per fsync divided by 8, e.g. 16 bit x 2 channels = 4 bytes")

        parser.add_argument(
            "--bclk-freq", metavar="FREQ", type=float,
            help="instead of using external clock, generate bit clock at FREQ Hz and derive fsync from it")

    def build(self, args):
        with self.assembly.add_applet(self):
            self.assembly.use_voltage(args.voltage)
            self.tdm_iface = TDMInterface(self.logger, self.assembly,
                bclk=args.bclk, fsync=args.fsync, rx=args.rx, tx=args.tx,
                frame_bytes=args.frame_bytes, bclk_generator_frequency=args.bclk_freq
            )

    @classmethod
    def add_run_arguments(cls, parser):
        # parser.add_argument(
        #     "-l", "--loop", action='store_true',
        #     help="loop the txfile until the command is aborted")

        # parser.add_argument(
        #     "--txfile", type=argparse.FileType('rb'),
        #     help="raw audio file to transmit")

        # parser.add_argument(
        #     "--rxfile", type=argparse.FileType('wb'),
        #     help="raw audio file to record to")
        pass

    async def run(self, args):
        for i in range(10):
            await self.tdm_iface.write_frame()
            print(await self.tdm_iface.read_frame())
        # write_future = asyncio.create_task(write_task())
        # read_future = asyncio.create_task(read_task(write_future))
        # await asyncio.gather(write_future, read_future)

    @classmethod
    def tests(cls):
        from . import test
        return test.TDMAppletTestCase
