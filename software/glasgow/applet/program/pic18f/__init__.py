# Reference: https://ww1.microchip.com/downloads/en/DeviceDoc/41342E.pdf
# Accession: TODO

import os
import math
import asyncio
import logging
import argparse
import struct
from fx2.format import input_data, output_data
from amaranth import *
from amaranth.lib import enum, data, wiring, stream, io
from amaranth.lib.wiring import In, Out

from ... import *
from ....support.logging import dump_hex
from glasgow.applet.control.gpio import GPIOInterface
from glasgow.abstract import AbstractAssembly, GlasgowPin, ClockDivisor
from glasgow.applet import GlasgowAppletError, GlasgowAppletV2

# Command structure in host -> glasgow fifo:
# byte 1: internal command
# byte 2: pic 4 bit command
# byte 3,4: pic 16bit payload

# Command structure in glasgow -> host fifo:
# byte 1: read byte

# Internal commands:
# bit 0 indicates pgd oe
# bit 1 indicates programming clock stretch
# bit 2 indicates erase clock stretch
CMD_READ          = 0b0000
CMD_WRITE         = 0b0001
CMD_WRITE_PROGRAM = 0b0011
CMD_WRITE_ERASE   = 0b0101


class DataShifter(wiring.Component):
    cmd_valid: In(1) # Indicates all In fields have been set by the outer component. They must be kept valid until cmd_done is set.
    cmd_done: Out(1) # Indicates the command has been sent on the bus and the Out fields are set by this component

    cmd: In(20)
    is_read: In(1)
    payload_read: Out(8)

    stretch_clk_programming: In(1) # Stretch the 4th clock cycle for programming
    stretch_clk_erase: In(1) # Stretch the 4th clock cycle for erase

    divisor: In(16)

    def __init__(self, ports, sys_clk_period):
        self._ports = ports
        self._sys_clk_period = sys_clk_period
        self._erase_duration = int(5.1e-3 / self._sys_clk_period)  # P11 + P10
        self._program_duration_high = int(1e-3 / self._sys_clk_period)  # P9 (TODO P9A for configuration word (what's that?))
        self._program_duration_low = int(.1e-3 / self._sys_clk_period)  # P10

        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules['buffer_pgc'] = buffer_pgc = io.Buffer("o", self._ports.pgc)
        m.submodules['buffer_pgd'] = buffer_pgd = io.Buffer("io", self._ports.pgd)

        ### Clock
        clock_cycles = Signal(10)
        timer = Signal(20)
        clock = Signal(1)
        m.d.comb += buffer_pgc.oe.eq(1)
        m.d.comb += clock.eq(clock_cycles[0])
        m.d.comb += buffer_pgc.o[0].eq(clock)

        cycle_duration = Signal(20)
        cycling = Signal(1)

        # Certain clock cycles are stretched for programming and erasing of flash lines
        with m.If(clock_cycles == 33):
            with m.If(self.stretch_clk_programming):
                m.d.comb += cycle_duration.eq(self._program_duration_high)
            with m.Else():
                m.d.comb += cycle_duration.eq(self.divisor)
        with m.Elif(clock_cycles == 32):
            with m.If(self.stretch_clk_erase):
                m.d.comb += cycle_duration.eq(self._erase_duration)
            with m.Elif(self.stretch_clk_programming):
                m.d.comb += cycle_duration.eq(self._program_duration_low)
            with m.Else():
                m.d.comb += cycle_duration.eq(self.divisor * 2)
        with m.Elif(clock_cycles == 0):
            m.d.comb += cycle_duration.eq(self.divisor * 2)
        with m.Else():
            m.d.comb += cycle_duration.eq(self.divisor)
        


        with m.If(cycling):
            with m.If(timer == cycle_duration):
                m.d.sync += timer.eq(0)
                with m.If(clock_cycles == 0):
                    m.d.sync += cycling.eq(0)
                with m.Else():
                    m.d.sync += clock_cycles.eq(clock_cycles - 1)
            with m.Else():
                m.d.sync += timer.eq(timer + 1)
        with m.Else():
            m.d.sync += timer.eq(0)

        # Enable data output while clock is active, unless we are in the last 8 bytes for a read
        m.d.comb += buffer_pgd.oe.eq(cycling & ((clock_cycles > 16) | (~self.is_read)))

        last_clock = Signal(1)
        m.d.sync += last_clock.eq(clock)

        rising = Signal(1)
        m.d.comb += rising.eq(~last_clock & clock)

        falling = Signal(1)
        m.d.comb += falling.eq(last_clock & ~clock)

        ### Shifting
        shreg_o = Signal(20)
        shreg_i = Signal(8)

        with m.If(rising): # shift out
            m.d.sync += buffer_pgd.o[0].eq(shreg_o[0])
            m.d.sync += shreg_o.eq(Cat(shreg_o[1:], C(0, 1)))

        with m.If(falling & (clock_cycles < 16)): # shift in 
            m.d.sync += shreg_i.eq(Cat(shreg_i[1:], buffer_pgd.i[0]))

        # Ideally pdg oe is enabled slightly after clock goes high (figure 5.5)


        ### State machine

        with m.FSM() as fsm:
            with m.State("IDLE"):
                with m.If(self.cmd_valid):
                    m.next = "SEND-CMD"
                    m.d.sync += clock_cycles.eq(20*2-1)
                    m.d.sync += cycling.eq(1)
                    m.d.sync += shreg_o.eq(self.cmd)
            with m.State("SEND-CMD"):
                with m.If(~cycling):
                    m.d.sync += self.payload_read.eq(shreg_i)
                    m.next = "DONE"
            with m.State("DONE"):
                m.d.comb += self.cmd_done.eq(1)
                m.next = "IDLE"

        return m


class ProgramPIC18fComponent(wiring.Component):
    i_stream: In(stream.Signature(8))
    o_stream: Out(stream.Signature(8))

    divisor: In(16)

    def __init__(self, ports, sys_clk_period):
        self._ports = ports
        self._sys_clk_period = sys_clk_period

        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.shifter = shifter = DataShifter(ports=self._ports, sys_clk_period=self._sys_clk_period)
        m.d.comb += shifter.divisor.eq(self.divisor)

        ### FIFO
        cmd_internal = Signal(4)

        with m.FSM() as fsm:
            with m.State("RECV-COMMAND"):
                m.d.comb += self.i_stream.ready.eq(1)
                with m.If(self.i_stream.valid):
                    m.d.sync += cmd_internal.eq(self.i_stream.payload[4:8])
                    m.d.sync += shifter.cmd[0:4].eq(self.i_stream.payload[0:4])
                    m.next = "RECV-PAYLOAD1"
            with m.State("RECV-PAYLOAD1"):
                m.d.comb += self.i_stream.ready.eq(1)
                with m.If(self.i_stream.valid):
                    m.d.sync += shifter.cmd[4:12].eq(self.i_stream.payload)
                    m.next = "RECV-PAYLOAD2"
            with m.State("RECV-PAYLOAD2"):
                m.d.comb += self.i_stream.ready.eq(1)
                with m.If(self.i_stream.valid):
                    m.d.sync += shifter.cmd[12:20].eq(self.i_stream.payload)
                    m.next = "EXEC"
            with m.State("EXEC"):
                m.d.comb += shifter.is_read.eq(~cmd_internal[0])
                m.d.comb += shifter.stretch_clk_programming.eq(cmd_internal[1])
                m.d.comb += shifter.stretch_clk_erase.eq(cmd_internal[2])
                m.d.comb += shifter.cmd_valid.eq(1)
                with m.If(shifter.cmd_done):
                    with m.If(shifter.is_read):
                        m.next = "SEND-PAYLOAD1"
                    with m.Else():
                        m.next = "RECV-COMMAND"
            with m.State("SEND-PAYLOAD1"):
                m.d.comb += [
                    self.o_stream.payload.eq(shifter.payload_read),
                    self.o_stream.valid.eq(1)
                ]
                with m.If(self.o_stream.ready):
                    m.next = "RECV-COMMAND"

        return m

class ProgramPIC18fInterface:
    def __init__(self, logger: logging.Logger, assembly: AbstractAssembly, *, pgd: GlasgowPin, pgc: GlasgowPin, pgm: GlasgowPin, mclr: GlasgowPin):
        self._logger = logger
        self._level  = logging.DEBUG if self._logger.name == __name__ else logging.TRACE
        self._pgm_iface = GPIOInterface(logger, assembly, pins=(pgm,))
        self._mclr_iface = GPIOInterface(logger, assembly, pins=(mclr,))

        # assembly.use_pulls({pgc: "high", pgd: "high"})
        ports = assembly.add_port_group(pgd=pgd, pgc=pgc)
        component = assembly.add_submodule(ProgramPIC18fComponent(ports, sys_clk_period=assembly.sys_clk_period))
        self._pipe = assembly.add_inout_pipe(component.o_stream, component.i_stream)
        self._clock = assembly.add_clock_divisor(component.divisor, ref_period=assembly.sys_clk_period * 2, name="pgc")

    def _log(self, message, *args):
        self._logger.log(self._level, "PIC18f: " + message, *args)

    async def enter_low_voltage_program_mode(self):
        await self._pgm_iface.output(0, True)
        # TODO Does such a short sleep even make sense?
        # TODO asyncio sleep is not allowed in tests, how do I make a sleep that works on real hardware and in tests?
        # Could use delay of PIC Component, that will also make sure GPIOs are synchronized with the rest, if that's not anyway the case
        # await asyncio.sleep(2e-6) # P15 = 2us
        await self._mclr_iface.output(0, True)
        # await asyncio.sleep(2e-6) # P12 = 2us

    async def exit_low_voltage_program_mode(self):
        await self.sync()
        # No sleep required, P16 = 0s
        await self._mclr_iface.output(0, True)
        # No sleep required, P18 = 0s
        await self._pgm_iface.output(0, True)

    async def read_n(self, cmd_4bit, n):
        cmd = CMD_READ << 4 | cmd_4bit & 0xf
        payload = 0x0000
        # TODO can this deadlock if n is too big to fit FIFO?
        await self._pipe.send(struct.pack("<BH", cmd, payload) * n)
        await self._pipe.flush()
        return await self._pipe.recv(n)

    async def write(self, cmd_4bit, payload):
        cmd = CMD_WRITE << 4 | cmd_4bit & 0xf
        await self._pipe.send(struct.pack("<BH", cmd, payload))

    async def write_program(self, cmd_4bit, payload):
        cmd = CMD_WRITE_PROGRAM << 4 | cmd_4bit & 0xf
        await self._pipe.send(struct.pack("<BH", cmd, payload))

    async def sync(self):
        # Do a read on a nop instruction
        cmd = CMD_READ << 4 | 0b0000
        payload = 0x0000
        await self._pipe.send(struct.pack("<BH", cmd, payload))
        await self._pipe.flush()
        await self._pipe.recv(1)

    async def run_commands(self, command_table):
        commands = bytearray()
        for (cmd_4bit, payload) in command_table:
            cmd = CMD_WRITE << 4 | cmd_4bit & 0xf
            commands.extend(struct.pack("<BH", cmd, payload))
        await self._pipe.send(commands)

    async def set_tblptr(self, addr):
        addr_h = (addr >> 16) & 0xff
        addr_m = (addr >> 8) & 0xff
        addr_l = (addr >> 0) & 0xff
        await self.run_commands([
            (0b0000, 0x0E << 8 | addr_h),
            (0b0000, 0x6EF8),
            (0b0000, 0x0E << 8 | addr_m),
            (0b0000, 0x6EF7),
            (0b0000, 0x0E << 8 | addr_l),
            (0b0000, 0x6EF6),
        ])

    async def erase(self):
        self._logger.info("Performing bulk chip erase")
        await self.run_commands([
            (0b0000, 0x0E3C), # MOVLW 3Ch
            (0b0000, 0x6EF8), # MOVWF TBLPTRU
            (0b0000, 0x0E00), # MOVLW 00h
            (0b0000, 0x6EF7), # MOVWF TBLPTRH
            (0b0000, 0x0E05), # MOVLW 05h
            (0b0000, 0x6EF6), # MOVWF TBLPTRL
            (0b1100, 0x0F0F), # Write 0Fh to 3C0005h
            (0b0000, 0x0E3C), # MOVLW 3Ch
            (0b0000, 0x6EF8), # MOVWF TBLPTRU
            (0b0000, 0x0E00), # MOVLW 00h
            (0b0000, 0x6EF7), # MOVWF TBLPTRH
            (0b0000, 0x0E04), # MOVLW 04h
            (0b0000, 0x6EF6), # MOVWF TBLPTRL
            (0b1100, 0x8F8F), # Write 8F8Fh TO 3C0004h to erase entire device. Other codes exist to erase certain sections
            (0b0000, 0x0000), # NOP
        ])

        cmd = CMD_WRITE_ERASE << 4 | 0b0000 & 0xf
        await self._pipe.send(struct.pack("<BH", cmd, 0))
    
    async def program_line(self, line_address, bytes):
        assert line_address % 16 == 0
        assert len(bytes) == 16 # TODO this is for PIC18F14K50

        await self.run_commands([
            (0b0000, 0x8EA6), # BSF EECON1, EEPGD
            (0b0000, 0x9CA6), # BCF EECON1, CFGS
            (0b0000, 0x84A6), # BSF EECON1, WREN
        ])
        await self.set_tblptr(line_address)

        commands = []
        for i in range(len(bytes)//2):
            byte1 = bytes[i * 2]
            byte2 = bytes[i * 2 + 1]
            if i == len(bytes)//2 - 1:
                commands.append((0b1111, byte2 << 8 | byte1))
            else:
                commands.append((0b1101, byte2 << 8 | byte1))
        await self.run_commands(commands)
        await self.write_program(0b0000, 0)

        # TODO configuration lines need a longer clock stretch

    async def read_memory(self, address, n_bytes):
        await self.set_tblptr(address)
        return await self.read_n(0b1001, n_bytes)

    async def read_device_id(self):
        devid1, devid2 = await self.read_memory(0x3ffffe, 2)
        return devid1, devid2


pic18f_device_ids = {
    0b000: "PIC18LF13K50",
    0b001: "PIC18LF14K50",
    0b010: "PIC18F13K50",
    0b011: "PIC18F14K50",
}

class ProgramPIC18fApplet(GlasgowAppletV2):
    logger = logging.getLogger(__name__)
    help = "program PIC18f microcontrollers"
    description = """
    """

    @classmethod
    def add_build_arguments(cls, parser, access):
        access.add_voltage_argument(parser)

        # Order matches the pin order, in clockwise direction.
        access.add_pins_argument(parser, "pgd",  default=True, required=True)
        access.add_pins_argument(parser, "pgc",  default=True, required=True)
        access.add_pins_argument(parser, "pgm",  default=True, required=True)
        access.add_pins_argument(parser, "mclr", default=True, required=True)

    def build(self, args):
        with self.assembly.add_applet(self):
            self.assembly.use_voltage(args.voltage)
            self.pic_iface = ProgramPIC18fInterface(self.logger, self.assembly, pgd=args.pgd, pgc=args.pgc, pgm=args.pgm, mclr=args.mclr)

    @classmethod
    def add_run_arguments(cls, parser):
        p_operation = parser.add_subparsers(dest="operation", metavar="OPERATION", required=True)

        p_device_id = p_operation.add_parser(
            "device-id", help="Read device id")

        p_erase = p_operation.add_parser(
            "erase", help="Erase whole chip memory")

        p_program = p_operation.add_parser(
            "program", help="program MCU memory contents")
        p_program.add_argument(
            "file", metavar="HEX-FILE", type=argparse.FileType("rb"),
            help="firmware file to read (in Intel HEX format)")

    async def run(self, args):
        try:
            await self.pic_iface._clock.set_frequency(1e6)
            await self.pic_iface.enter_low_voltage_program_mode()

            # TODO: get from device id
            line_length = 16

            if args.operation == "device-id":
                devid1, devid2 = await self.pic_iface.read_device_id()
                self.logger.info(f"DEVID1: %#02x, DEVID2: %#02x", devid1, devid2)
                if devid2 != 0x47:
                    self.logger.error("Failed to read device id")
                else:
                    try:
                        self.logger.info(f"Device: %s", pic18f_device_ids[devid1 >> 5])
                    except KeyError:
                        self.logger.error("Unknown device")

            if args.operation == "erase":
                await self.pic_iface.erase()

            if args.operation == "program":
                def ceildiv(a, b):
                    return -(a // -b)

                class FlashLine:
                    read_data = 0
                    write_data = 0
                    write_mask = 0

                    def update(self, new_write, new_mask):
                        overlap = (new_mask & self.write_mask)
                        assert (self.write_data & overlap) == (new_write & overlap)
                        self.write_data = self.write_data & self.write_mask | new_write & new_mask 

                all_ones = int.from_bytes([0xff] * line_length)

                # Make a list of all lines we want to (partially) write to
                lines = {} # key: line address, value = (read data, write data, write_mask)
                for chunk_mem_addr, chunk_data in sorted(input_data(args.file, fmt="ihex"),
                                                         key=lambda c: c[0]):
                    if len(chunk_data) == 0: continue
                    first_full_line_number = ceildiv(chunk_mem_addr, line_length)
                    last_full_line_number_exclusive = (chunk_mem_addr + len(chunk_data) - 1) // line_length

                    pre_extra_bytes = first_full_line_number * line_length - chunk_mem_addr
                    post_extra_bytes = (chunk_mem_addr + len(chunk_data)) - last_full_line_number_exclusive * line_length

                    if pre_extra_bytes > 0:
                        line = lines.setdefault(first_full_line_number - 1, FlashLine())
                        line_data = chunk_data[0:pre_extra_bytes]
                        write_data = int.from_bytes([0x00] * (line_length - pre_extra_bytes) + line_data)
                        write_mask = int.from_bytes([0x00] * (line_length - pre_extra_bytes) + [0xff] * pre_extra_bytes)
                        line.update(write_data, write_mask)

                    for line_number in range(first_full_line_number, last_full_line_number_exclusive):
                        line = lines.setdefault(line_number, FlashLine())
                        line_data = chunk_data[line_number*16+pre_extra_bytes:(line_number + 1)*16+pre_extra_bytes]
                        write_data = int.from_bytes(line_data)
                        write_mask = all_ones
                        line.update(write_data, write_mask)

                    if post_extra_bytes > 0:
                        line = lines.setdefault(last_full_line_number_exclusive, FlashLine())
                        line_data = chunk_data[-post_extra_bytes:]
                        write_data = int.from_bytes(line_data + [0x00] * (line_length - post_extra_bytes))
                        write_mask = int.from_bytes([0xff] * post_extra_bytes + [0x00] * (line_length - post_extra_bytes))
                        line.update(write_data, write_mask)

                self.logger.info("Reading memory to check write compatibility")
                for line_number, line in lines.items():
                    line_read = await self.pic_iface.read_memory(line_number * line_length, line_length)
                    line.read_data = int.from_bytes(line_read)

                    # Check that the write is compatible with the data already in memory (no further erase needed)
                    write_ok = (line.read_data | all_ones - line.write_data | all_ones - line.write_mask) == all_ones
                    if not write_ok:
                        print(hex(line_number * line_length))
                        print(line.read_data.to_bytes(line_length), line.write_data.to_bytes(line_length), line.write_mask.to_bytes(line_length))
                        raise RuntimeError("Incompatible write into a memory location that was not erased")

                self.logger.info("Writing %d lines / %d bytes of memory", len(lines), len(lines) * line_length)
                for line_number, line in lines.items():
                    line_data = (line.read_data & (all_ones - line.write_mask)) | (line.write_data & line.write_mask)
                    if line_data != line.read_data: # Check if there is any data to write
                        await self.pic_iface.program_line(line_number * line_length, line_data.to_bytes(line_length))

                await self.pic_iface.sync() # wait for writes to be done before printing message
                self.logger.info("Reading back memory")
                for line_number, line in lines.items():
                    line_data = (line.read_data & (all_ones - line.write_mask)) | (line.write_data & line.write_mask)
                    readback = await self.pic_iface.read_memory(line_number * line_length, line_length)
                    if readback != line_data.to_bytes(line_length):
                        raise RuntimeError(f"Incorrect readback at address 0x{line_number * line_length:06x}")


                self.logger.info("Reading back memory again to check internal logic")
                for chunk_mem_addr, chunk_data in sorted(input_data(args.file, fmt="ihex"),
                                                         key=lambda c: c[0]):
                    readback = await self.pic_iface.read_memory(chunk_mem_addr, chunk_data)
                    assert readback.tobytes() == chunk_data


        finally:
            await self.pic_iface.exit_low_voltage_program_mode()

    @classmethod
    def tests(cls):
        from . import test
        return test.ProgramPIC18fAppletTestCase
