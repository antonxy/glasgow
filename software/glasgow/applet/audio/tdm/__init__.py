import logging
import asyncio
import argparse
from amaranth import *
from amaranth.lib import io, fifo

from ... import *
from ....gateware.clockgen import ClockGen

class TDMBus(Elaboratable):
    def __init__(self, ports, clock_dir):
        self.tx_buffer = io.Buffer("o", ports.tx) if hasattr(ports, 'tx') else None
        self.rx_buffer = io.Buffer("i", ports.rx) if hasattr(ports, 'rx') else None
        dir = 'o' if clock_dir == 'source' else 'i'
        self.bclk_buffer = io.Buffer(dir, ports.bclk)
        self.fsync_buffer = io.Buffer(dir, ports.fsync)
    
    def elaborate(self, platform):
        m = Module()
        if self.tx_buffer:
            m.submodules.tx_buffer = self.tx_buffer
        if self.rx_buffer:
            m.submodules.rx_buffer = self.rx_buffer
        m.submodules.bclk_buffer = self.bclk_buffer
        m.submodules.fsync_buffer = self.fsync_buffer
        return m

class TDMSubtarget(Elaboratable):
    def __init__(self, ports, out_fifo, in_fifo, clock_dir, bclk_cyc, n_channels, n_bits, output_fsync_delay, fsync_duration, extra_cycles):
        self.ports = ports
        self.out_fifo = out_fifo
        self.in_fifo = in_fifo
        self.clock_dir = clock_dir
        self.bclk_cyc = bclk_cyc
        self.n_channels = n_channels
        self.n_bits = n_bits
        self.output_fsync_delay = output_fsync_delay
        self.fsync_duration = fsync_duration
        self.extra_cycles = extra_cycles

        self.bus = TDMBus(ports, clock_dir)

    def elaborate(self, platform):
        m = Module()

        m.submodules.bus = self.bus

        bits_per_frame = self.n_channels * self.n_bits
        bytes_per_frame = self.n_channels * self.n_bits // 8

        m.domains.bclk = cd_bclk = ClockDomain("bclk", local=True)
        fsync = Signal()

        # What happens if bclk and fsync are not perfectly synchonized?

        # TODO detect buffer underflows / overflows and indicate error
        # TODO detect errors such as too early fsync

        fsync_counter = Signal(range(bits_per_frame * 4))

        # Generate or import bit clock
        if self.clock_dir == 'source':
            # TODO maybe better to use a PLL?
            m.submodules.clockgen = ClockGen(self.bclk_cyc)
            m.d.comb += cd_bclk.clk.eq(m.submodules.clockgen.clk)

            # TODO add an enable that starts the clocks only once data is ready in the queue
            # and maybe also stop them once we are done

            frame_duration = bits_per_frame + self.extra_cycles
            
            # If we are the clock source fsync is driven by the fsync counter
            with m.If(fsync_counter < frame_duration - 1):
                m.d.bclk += fsync_counter.eq(fsync_counter + 1)
            with m.Else():
                m.d.bclk += fsync_counter.eq(0)
            m.d.comb += fsync.eq(fsync_counter < self.fsync_duration)

            m.d.comb += self.bus.bclk_buffer.o.eq(m.submodules.clockgen.clk)
            m.d.comb += self.bus.fsync_buffer.o.eq(fsync)
        else:
            assert self.clock_dir == 'sink'
            m.d.comb += cd_bclk.clk.eq(self.bus.bclk_buffer.i)
            m.d.comb += fsync.eq(self.bus.fsync_buffer.i)

            # If we are the clock sink the fsync_counter is driven by fsync
            # TODO this should happen immediately, not after one bclk
            m.d.bclk += fsync_counter.eq(Mux(fsync, 0, Mux(fsync_counter < 2**len(fsync_counter)-2, fsync_counter + 1, fsync_counter)))
            # TODO could report number of bits per fsync here
            # TODO Also doesn't behave correctly with fsync longer that 1 bclk, should use the fsync_rising signal

        # do we actually need those or could we use self.out_fifo._fifo.r_level? self.out_fifo might not be async though

        # fifos for transfer between sync domain and bclk domain
        
        if self.bus.tx_buffer != None:
            m.submodules.o_fifo = o_fifo = fifo.AsyncFIFO(width=8, depth=bytes_per_frame, r_domain='bclk', w_domain='sync')
            # Fill o_fifo from self.out_fifo
            with m.If(o_fifo.w_rdy & self.out_fifo.r_rdy):
                m.d.comb += o_fifo.w_en.eq(1)
                m.d.comb += self.out_fifo.r_en.eq(1)
                m.d.sync += o_fifo.w_data.eq(self.out_fifo.r_data) # TODO why is this one sync, but in the other direction comb? Works, but suspicious

            full_frame_in_fifo = Signal()
            m.d.comb += full_frame_in_fifo.eq(m.submodules.o_fifo.r_level >= bytes_per_frame - 2) # -2 because we keep 1 byte in the fifo out register, I think. And one somewhere else? Maybe I have to actually think this through
        else:
            full_frame_in_fifo = Const(1)

        if self.bus.rx_buffer != None:
            m.submodules.i_fifo = i_fifo = fifo.AsyncFIFO(width=8, depth=bytes_per_frame, r_domain='sync', w_domain='bclk')
            # Empty i_fifo into self.in_fifo
            with m.If(i_fifo.r_rdy & self.in_fifo.w_rdy):
                m.d.comb += i_fifo.r_en.eq(1)
                m.d.comb += self.in_fifo.w_en.eq(1)
                m.d.comb += self.in_fifo.w_data.eq(i_fifo.r_data)

            space_for_full_frame_in_fifo = Signal()
            m.d.comb += space_for_full_frame_in_fifo.eq(m.submodules.i_fifo.w_level == 0)
        else:
            space_for_full_frame_in_fifo = Const(1)

        # Limit fsync length to one bclk internally
        fsync_reg = Signal()
        m.d.bclk += fsync_reg.eq(fsync)
        fsync_rising = Signal()
        m.d.comb += fsync_rising.eq(fsync & ~fsync_reg)

        active_frame = Signal()
        with m.If(fsync_rising):
            # Set the frame as active only if we have a full frame of data available in the fifo
            # Otherwise we might read part of a frame from the fifo and mess up the framing
            m.d.bclk += active_frame.eq(full_frame_in_fifo & space_for_full_frame_in_fifo)
            # TODO maybe the inverse for i_fifo. Only activate if there is space for a full frame
            # TODO maybe we could count frame drops here and report them

        # If output_fsync_delay is set to zero we want full_frame_in_fifo to immediately set active_frame, not after one bclk
        active_frame_imm = Signal()
        if self.output_fsync_delay == 0:
            m.d.comb += active_frame_imm.eq(Mux(fsync_rising, full_frame_in_fifo & space_for_full_frame_in_fifo, active_frame))
            # TODO active_frame_imm maybe also has to deassert one bclk earlier than active_frame
        elif self.output_fsync_delay == 1:
            m.d.comb += active_frame_imm.eq(active_frame)
        else:
            assert False
        
        # Stop shifting data in/out if we have reached the number of bits per frame
        # TODO maybe this can be combined into active_frame_imm
        still_shifting = Signal()
        m.d.comb += still_shifting.eq(fsync_counter < bits_per_frame + self.output_fsync_delay)

        # TODO detect if fsync comes before bits per frame is reached
        # In that case we have to fill the fifo with zeros
        # Or have a mechanism to drop a frame before we send it out


        if self.bus.tx_buffer != None:
            bits_valid_o = Signal(range(9))
            shreg_o = Signal(8)
            m.d.comb += self.bus.tx_buffer.o.eq(shreg_o[-1] & active_frame_imm & still_shifting)

            # Shift data out of shreg_o and into shreg_i on every bclk
            with m.If(active_frame_imm & still_shifting):
                m.d.bclk += shreg_o.eq(Cat(C(0, 1), shreg_o))
                m.d.bclk += bits_valid_o.eq(bits_valid_o - 1)
            
            # Get new transmit byte
            #TODO did I misunderstand fifo docs here? r_rdy means that data is already in r_data and r_en will put in the next one
            # or does it? I see r_rdy asserted and r_data contains nothing
            # maybe this mistake and the comb sync thing above cancel each other?
            # maybe this can be simplified by fixing both
            with m.FSM(name="shreg_o_refill", domain="bclk"):
                with m.State("Wait"):
                    # Read a byte from the fifo as soon as it is available, it will be kept in r_data until we need it
                    with m.If(o_fifo.r_rdy):
                        m.d.comb += o_fifo.r_en.eq(1)
                        m.next = "Read"
                with m.State("Read"):
                    # When the shift register is empty refill it from r_data
                    with m.If(bits_valid_o <= 1):
                        m.d.bclk += shreg_o.eq(o_fifo.r_data)
                        m.d.bclk += bits_valid_o.eq(8)

                        m.next = "Wait"

        if self.bus.rx_buffer != None:
            bits_valid_i = Signal(range(9))
            shreg_i = Signal(8)

            with m.If(active_frame_imm & still_shifting):
                m.d.bclk += shreg_i.eq(Cat(self.bus.rx_buffer.i, shreg_i))
                m.d.bclk += bits_valid_i.eq(bits_valid_i + 1)
            
            # Send out received byte
            with m.If(bits_valid_i >= 8):
                m.d.bclk += i_fifo.w_data.eq(shreg_i)
                m.d.bclk += i_fifo.w_en.eq(1)
                with m.If(active_frame_imm & still_shifting):  # If we also shifted in data in this cycle
                    m.d.bclk += bits_valid_i.eq(1)
                with m.Else():
                    m.d.bclk += bits_valid_i.eq(0)
            with m.Else():
                m.d.bclk += i_fifo.w_en.eq(0)
        

        return m

class TDMApplet(GlasgowApplet):
    logger = logging.getLogger(__name__)
    help = "Send and receive audio data via TDM/I2S"
    description = """
    """

    @classmethod
    def add_build_arguments(cls, parser, access):
        super().add_build_arguments(parser, access)

        access.add_pin_argument(parser, "rx")
        access.add_pin_argument(parser, "tx")
        access.add_pin_argument(parser, "bclk", required=True)
        access.add_pin_argument(parser, "fsync", required=True)

        parser.add_argument(
            "--clock-dir", metavar="DIR", choices=("source", "sink"),
            default="source",
            help="set clock direction as DIR (default: %(default)s)")
        parser.add_argument(
            "-r", "--sample-rate", metavar="FREQ", type=int, default=48000,
            help="set sample rate to FREQ Hz (default: %(default)s)")
        parser.add_argument(
            "-c", "--channels", type=int, default=2,
            help="number of channels (default: %(default)s)")
        parser.add_argument(
            "-d", "--bit-depth", type=int, default=16,
            help="bit depth, only multiples of 8 are supported (default: %(default)s)")
        parser.add_argument(
            "--fsync-delay", metavar="DEL", choices=(0, 1), default=0,
            help="output is valid DEL bclk cycles after fsync rising edge (default: %(default)s)")
        
        # TODO these only have an effect in clock souce mode, complain if they are set in sink mode
        parser.add_argument(
            "--fsync-duration", type=int, default=1,
            help="duration of the generated fsync pulse in bclk cycles (default: %(default)s)")
        parser.add_argument(
            "--extra-cycles", type=int, default=0,
            help="number of bclk cycles after a frame is sent before the next fsync pulse in generated (default: %(default)s)")

    def build(self, target, args, name=None):
        assert args.bit_depth % 8 == 0
        bclk_frequency = args.sample_rate * args.channels * args.bit_depth
        bclk_cyc = self.derive_clock(input_hz=target.sys_clk_freq, output_hz=bclk_frequency)
        # TODO we could make fclk more accurate if it doesn't have to be an exact multiple of bclk maybe

        assert args.fsync_duration > 0 and args.fsync_duration < args.channels * args.bit_depth

        ports = {
            'rx': args.pin_rx,
            'tx': args.pin_tx,
            'bclk': args.pin_bclk,
            'fsync': args.pin_fsync,
        }

        self.mux_interface = iface = target.multiplexer.claim_interface(self, args)
        subtarget = iface.add_subtarget(TDMSubtarget(
            ports=iface.get_port_group(**{k:v for k,v in ports.items() if v is not None}),
            out_fifo=iface.get_out_fifo(),
            in_fifo=iface.get_in_fifo(),
            clock_dir=args.clock_dir,
            bclk_cyc=bclk_cyc,
            n_channels=args.channels,
            n_bits=args.bit_depth,
            output_fsync_delay=args.fsync_delay,
            fsync_duration=args.fsync_duration,
            extra_cycles=args.extra_cycles
            ))
        
        # TODO: currently the FIFO immediately overruns before any data was sent. Workaround: reset device so that the applet has to be loaded first
        if hasattr(target, 'analyzer') and target.analyzer is not None:
            target.analyzer.add_pin_event(self, "tx", subtarget.bus.tx_buffer)
            target.analyzer.add_pin_event(self, "rx", subtarget.bus.rx_buffer)
            target.analyzer.add_pin_event(self, "bclk", subtarget.bus.bclk_buffer)
            target.analyzer.add_pin_event(self, "fsync", subtarget.bus.fsync_buffer)

        return subtarget

    @classmethod
    def add_run_arguments(cls, parser, access):
        super().add_run_arguments(parser, access)

    async def run(self, device, args):
        iface = await device.demultiplexer.claim_interface(self, self.mux_interface, args)
        return iface
    
    @classmethod
    def add_interact_arguments(cls, parser):
        parser.add_argument(
            "-l", "--loop", action='store_true',
            help="loop the txfile until the command is aborted")

        parser.add_argument(
            "--txfile", type=argparse.FileType('rb'),
            help="raw audio file to transmit")

        parser.add_argument(
            "--rxfile", type=argparse.FileType('wb'),
            help="raw audio file to record to")

    async def interact(self, device, args, iface):
        # TODO It looks like at least one byte is stuck in the applet at the end and not returned
        # It will be returned at the beginning of the next run

        async def write_task():
            if args.txfile:
                pcm_data = args.txfile.read()
                bytes_written = 0
                while True:
                    bytes_written += len(pcm_data)
                    await iface.write(pcm_data)
                    # Somehow if I don't call flush here nothing happens even though the writes get submitted
                    # I think it would make sense if I would only have to call flush once in the very end
                    await iface.flush(wait=False) 
                    if not args.loop:
                        break
                await iface.flush(wait=False)
                return bytes_written

        async def read_task(write_future):
            if args.rxfile:
                bytes_read = 0
                while not write_future.done() or bytes_read < write_future.result():

                    #TODO Can this get stuck waiting for one more byte even though the condition is satisfied somehow?
                    # flush=False has to be set, otherwise it can deadlock with the write task I think
                    d = bytes(await iface.read(flush=False)) 
                    bytes_read += len(d)
                    args.rxfile.write(d)

        write_future = asyncio.create_task(write_task())
        read_future = asyncio.create_task(read_task(write_future))
        await asyncio.gather(write_future, read_future)

    @classmethod
    def tests(cls):
        from . import test
        return test.TDMAppletTestCase