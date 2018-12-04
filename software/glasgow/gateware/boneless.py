from migen import *
from migen.fhdl.bitcontainer import value_bits_sign
from migen.fhdl.specials import _MemoryPort
from migen.genlib.fsm import *

from ..arch.boneless.opcode import *


__all__ = ["BonelessCore"]


def AddSignedImm(v, i):
    i_nbits, i_sign = value_bits_sign(i)
    if i_nbits > v.nbits:
        return v + i
    else:
        return v + Cat(i, Replicate(i[i_nbits - 1], v.nbits - i_nbits))


class _StubMemoryPort(Module):
    def __init__(self, name):
        self.adr   = Signal(16, name=name + "_adr")
        self.re    = Signal(1,  name=name + "_re")
        self.dat_r = Signal(16, name=name + "_dat_r")
        self.we    = Signal(1,  name=name + "_we")
        self.dat_w = Signal(16, name=name + "_dat_w")


class BonelessCore(Module):
    def __init__(self, reset_addr, mem_rdport, mem_wrport, ext_port=None, simulation=False):
        if ext_port is None:
            ext_port = _StubMemoryPort("ext")

        def decode(v):
            d = Signal.like(v)
            self.comb += d.eq(v)
            return d

        pc_bits = max(mem_rdport.adr.nbits, mem_wrport.adr.nbits)

        r_insn  = Signal(16)
        r_pc    = Signal(pc_bits, reset=reset_addr)
        r_win   = Signal(max(pc_bits - 3, 1))
        r_z     = Signal()
        r_s     = Signal()
        r_c     = Signal()
        r_o     = Signal()

        r_opA   = Signal(16)
        s_opB   = Signal(16)

        r_opS   = Signal(16)
        r_shift = Signal(5)

        s_res   = Signal(17)

        s_addr  = Signal(16)
        r_addr  = Signal(16)

        s_insn  = Signal(16)
        i_type1 = decode(s_insn[0:1])
        i_type2 = decode(s_insn[0:2])
        i_shift = decode(s_insn[1:5])
        i_imm5  = decode(s_insn[0:5])
        i_imm8  = decode(s_insn[0:8])
        i_imm11 = decode(s_insn[0:11])
        i_regX  = decode(s_insn[2:5])
        i_regY  = decode(s_insn[5:8])
        i_regZ  = decode(s_insn[8:11])
        i_code1 = decode(s_insn[11:12])
        i_code2 = decode(s_insn[11:13])
        i_code3 = decode(s_insn[11:14])
        i_code5 = decode(s_insn[11:16])
        i_store = decode(s_insn[11])
        i_ext   = decode(s_insn[12])
        i_flag  = decode(s_insn[11])
        i_cond  = decode(s_insn[12:15])

        i_clsA  = decode(i_code5[1:5] == OPCLASS_A)
        i_clsS  = decode(i_code5[1:5] == OPCLASS_S)
        i_clsM  = decode(i_code5[2:5] == OPCLASS_M)
        i_clsI  = decode(i_code5[3:5] == OPCLASS_I)
        i_clsC  = decode(i_code5[4:5] == OPCLASS_C)

        s_cond  = Signal()
        self.comb += [
            Case(Cat(i_cond, C(1, 1)), {
                OPCODE_F_0:     s_cond.eq(0),
                OPCODE_F_Z:     s_cond.eq(r_z),
                OPCODE_F_S:     s_cond.eq(r_s),
                OPCODE_F_O:     s_cond.eq(r_o),
                OPCODE_F_C:     s_cond.eq(r_c),
                OPCODE_F_CoZ:   s_cond.eq(r_c | r_z),
                OPCODE_F_SxO:   s_cond.eq(r_s ^ r_o),
                OPCODE_F_SxOoZ: s_cond.eq((r_s ^ r_o) | r_z),
            })
        ]

        s_sub   = Signal()
        s_cmp   = Signal()
        c_flags = Signal()
        self.sync += [
            If(c_flags,
                r_z.eq(s_res == 0),
                r_s.eq(s_res[15]),
                r_c.eq(s_res[16]),
                # http://teaching.idallen.com/cst8214/08w/notes/overflow.txt
                Case(Cat(s_sub | s_cmp, r_opA[15], s_opB[15], s_res[15]), {
                    0b1000: r_o.eq(1),
                    0b0110: r_o.eq(1),
                    0b1101: r_o.eq(1),
                    0b0011: r_o.eq(1),
                    "default": r_o.eq(0),
                })
            )
        ]

        self.comb += mem_rdport.re.eq(1)

        self.submodules.fsm = FSM(reset_state="FETCH")
        self.comb += [
            s_insn.eq(Mux(self.fsm.ongoing("DECODE/LOAD/JUMP"), mem_rdport.dat_r, r_insn)),
        ]
        self.fsm.act("FETCH",
            mem_rdport.adr.eq(r_pc),
            NextValue(r_pc, r_pc + 1),
            NextState("DECODE/LOAD/JUMP")
        )
        self.fsm.act("DECODE/LOAD/JUMP",
            NextValue(r_insn, mem_rdport.dat_r),
            If(i_clsA,
                mem_rdport.adr.eq(Cat(i_regX, r_win)),
                NextState("A-READ")
            ).Elif(i_clsS,
                mem_rdport.adr.eq(Cat(i_regY, r_win)),
                NextState("S-READ")
            ).Elif(i_clsM,
                mem_rdport.adr.eq(Cat(i_regY, r_win)),
                If(~i_store,
                    NextState("M/I-LOAD-1")
                ).Else(
                    NextState("M/I-STORE-1")
                )
            ).Elif(i_clsI,
                mem_rdport.adr.eq(Cat(i_regZ, r_win)),
                Case(Cat(i_code3, C(OPCLASS_I, 2)), {
                    OPCODE_MOVL: NextState("I-EXECUTE-MOVx/ADDI"),
                    OPCODE_MOVH: NextState("I-EXECUTE-MOVx/ADDI"),
                    OPCODE_MOVA: NextState("I-EXECUTE-MOVx/ADDI"),
                    OPCODE_ADDI: NextState("I-EXECUTE-MOVx/ADDI"),
                    OPCODE_LDI:  NextState("M/I-LOAD-1"),
                    OPCODE_STI:  NextState("M/I-STORE-1"),
                    OPCODE_JAL:  NextState("I-EXECUTE-JAL"),
                    OPCODE_JR:   NextState("I-EXECUTE-JR"),
                })
            ).Elif(i_clsC,
                If(s_cond == i_flag,
                    NextValue(r_pc, AddSignedImm(r_pc, i_imm11))
                ),
                NextState("FETCH"),
                If(simulation & (i_imm11 == 0x400),
                    NextState("HALT")
                )
            )
        )
        self.fsm.act("A-READ",
            mem_rdport.adr.eq(Cat(i_regY, r_win)),
            NextValue(r_opA, mem_rdport.dat_r),
            NextState("A-EXECUTE")
        )
        self.fsm.act("A-EXECUTE",
            s_opB.eq(mem_rdport.dat_r),
            Case(Cat(i_code1, C(OPCLASS_A, 4)), {
                OPCODE_LOGIC: Case(i_type2, {
                    OPTYPE_AND:  s_res.eq(r_opA & s_opB),
                    OPTYPE_OR:   s_res.eq(r_opA | s_opB),
                    OPTYPE_XOR:  s_res.eq(r_opA ^ s_opB),
                }),
                OPCODE_ARITH: Case(i_type2, {
                    OPTYPE_ADD:  s_res.eq(r_opA + s_opB),
                    OPTYPE_SUB: [s_res.eq(r_opA - s_opB), s_sub.eq(1)],
                    OPTYPE_CMP: [s_res.eq(r_opA - s_opB), s_cmp.eq(1)],
                })
            }),
            mem_wrport.adr.eq(Cat(i_regZ, r_win)),
            mem_wrport.dat_w.eq(s_res),
            mem_wrport.we.eq(~s_cmp),
            c_flags.eq(1),
            NextState("FETCH")
        )
        self.fsm.act("S-READ",
            NextValue(r_opS, mem_rdport.dat_r),
            NextValue(r_shift, i_shift),
            NextState("S-EXECUTE")
        )
        self.fsm.act("S-EXECUTE",
            s_res.eq(r_opS),
            mem_wrport.adr.eq(Cat(i_regZ, r_win)),
            mem_wrport.dat_w.eq(s_res),
            mem_wrport.we.eq(1),
            c_flags.eq(1),
            Case(Cat(i_code1, C(OPCLASS_S, 4)), {
                OPCODE_SHIFT_L: Case(i_type1, {
                    OPTYPE_SLL: NextValue(r_opS, Cat(C(0, 1),   r_opS[:-1])),
                    OPTYPE_ROT: NextValue(r_opS, Cat(r_opS[-1], r_opS[:-1])),
                }),
                OPCODE_SHIFT_R: Case(i_type1, {
                    OPTYPE_SRL: NextValue(r_opS, Cat(r_opS[1:], C(0, 1))),
                    OPTYPE_SRA: NextValue(r_opS, Cat(r_opS[1:], r_opS[-1])),
                })
            }),
            NextValue(r_shift, r_shift - 1),
            If(r_shift == 0,
                NextState("FETCH")
            )
        )
        self.fsm.act("M/I-LOAD-1",
            If(i_clsI,
                s_addr.eq(AddSignedImm(r_pc, i_imm8))
            ).Else(
                s_addr.eq(AddSignedImm(mem_rdport.dat_r, i_imm5))
            ),
            mem_rdport.adr.eq(s_addr),
            ext_port.adr.eq(s_addr),
            ext_port.re.eq(i_ext),
            NextState("M/I-LOAD-2")
        )
        self.fsm.act("M/I-LOAD-2",
            mem_wrport.adr.eq(Cat(i_regZ, r_win)),
            mem_wrport.dat_w.eq(Mux(i_ext, ext_port.dat_r, mem_rdport.dat_r)),
            mem_wrport.we.eq(1),
            NextState("FETCH")
        )
        self.fsm.act("M/I-STORE-1",
            If(i_clsI,
                NextValue(r_addr, AddSignedImm(r_pc, i_imm8))
            ).Else(
                NextValue(r_addr, AddSignedImm(mem_rdport.dat_r, i_imm5))
            ),
            mem_rdport.adr.eq(Cat(i_regZ, r_win)),
            NextState("M/I-STORE-2")
        )
        self.fsm.act("M/I-STORE-2",
            mem_wrport.adr.eq(r_addr),
            mem_wrport.dat_w.eq(mem_rdport.dat_r),
            mem_wrport.we.eq(~i_ext),
            ext_port.adr.eq(r_addr),
            ext_port.dat_w.eq(mem_rdport.dat_r),
            ext_port.we.eq(i_ext),
            NextState("FETCH")
        )
        self.fsm.act("I-EXECUTE-MOVx/ADDI",
            mem_wrport.adr.eq(Cat(i_regZ, r_win)),
            Case(Cat(i_code2, C(0b0, 1), C(OPCLASS_I, 2)), {
                OPCODE_MOVL: mem_wrport.dat_w.eq(Cat(i_imm8, C(0, 8))),
                OPCODE_MOVH: mem_wrport.dat_w.eq(Cat(C(0, 8), i_imm8)),
                OPCODE_MOVA: mem_wrport.dat_w.eq(AddSignedImm(r_pc, i_imm8)),
                OPCODE_ADDI: mem_wrport.dat_w.eq(AddSignedImm(mem_rdport.dat_r, i_imm8)),
            }),
            mem_wrport.we.eq(1),
            NextState("FETCH")
        )
        self.fsm.act("I-EXECUTE-JAL",
            mem_wrport.adr.eq(Cat(i_regZ, r_win)),
            mem_wrport.dat_w.eq(r_pc),
            mem_wrport.we.eq(1),
            NextValue(r_pc, AddSignedImm(r_pc, i_imm11)),
            NextState("FETCH")
        )
        self.fsm.act("I-EXECUTE-JR",
            NextValue(r_pc, AddSignedImm(mem_rdport.dat_r, i_imm11)),
            NextState("FETCH")
        )
        self.fsm.act("HALT",
            NextState("HALT")
        )

# -------------------------------------------------------------------------------------------------

import unittest

from . import simulation_test
from ..arch.boneless.instr import *


class BonelessSimulationTestbench(Module):
    def __init__(self):
        self.mem_init = []
        self.ext_init = []

    def do_finalize(self):
        self.mem = Memory(width=16, depth=len(self.mem_init), init=self.mem_init)
        self.specials += self.mem

        mem_rdport = self.mem.get_port(has_re=True, mode=READ_FIRST)
        mem_wrport = self.mem.get_port(write_capable=True)
        self.specials += [mem_rdport, mem_wrport]

        if self.ext_init:
            self.ext = Memory(width=16, depth=len(self.ext_init), init=self.ext_init)
            self.specials += self.ext

            ext_port = self.ext.get_port(has_re=True, write_capable=True)
            self.specials += ext_port
        else:
            ext_port = _StubMemoryPort("ext")

        self.submodules.dut = BonelessCore(reset_addr=8,
            mem_rdport=mem_rdport,
            mem_wrport=mem_wrport,
            ext_port=ext_port,
            simulation=True)


class BonelessTestCase(unittest.TestCase):
    def setUp(self):
        self.tb = BonelessSimulationTestbench()

    def configure(self, tb, code, regs=[], data=[], extr=[]):
        tb.mem_init = [*regs, *[0] * (8 - len(regs))] + assemble(code + [J(-1024)] + data)
        tb.ext_init = extr

    def dut_state(self, tb):
        return tb.dut.fsm.decoding[(yield tb.dut.fsm.state)]

    def run_core(self, tb):
        while (yield from self.dut_state(tb)) != "HALT":
            yield

    def assertMemory(self, tb, addr, value):
        self.assertEqual((yield tb.mem[addr]), value)

    def assertExternal(self, tb, addr, value):
        self.assertEqual((yield tb.ext[addr]), value)

    @simulation_test(regs=[0xA5A5, 0xAA55],
                     code=[AND (R2, R1, R0)])
    def test_AND(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0xA5A5)
        yield from self.assertMemory(tb, 1, 0xAA55)
        yield from self.assertMemory(tb, 2, 0xA005)

    @simulation_test(regs=[0xA5A5, 0xAA55],
                     code=[OR  (R2, R1, R0)])
    def test_OR(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0xA5A5)
        yield from self.assertMemory(tb, 1, 0xAA55)
        yield from self.assertMemory(tb, 2, 0xAFF5)

    @simulation_test(regs=[0xA5A5, 0xAA55],
                     code=[XOR (R2, R1, R0)])
    def test_XOR(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0xA5A5)
        yield from self.assertMemory(tb, 1, 0xAA55)
        yield from self.assertMemory(tb, 2, 0x0FF0)

    @simulation_test(regs=[0x1234, 0x5678],
                     code=[ADD (R2, R1, R0)])
    def test_ADD(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x1234)
        yield from self.assertMemory(tb, 1, 0x5678)
        yield from self.assertMemory(tb, 2, 0x68AC)

    @simulation_test(regs=[0x1234, 0x5678],
                     code=[SUB (R2, R1, R0)])
    def test_SUB(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x1234)
        yield from self.assertMemory(tb, 1, 0x5678)
        yield from self.assertMemory(tb, 2, 0xBBBC)

    @simulation_test(regs=[0x1234, 0x5678],
                     code=[CMP (R0, R1)])
    def test_CMP(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x1234)
        yield from self.assertMemory(tb, 1, 0x5678)
        yield from self.assertMemory(tb, 2, 0)

    @simulation_test(regs=[0x1012],
                     code=[SLL (R1, R0, 1),
                           SLL (R2, R0, 8)])
    def test_SLL(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x1012)
        yield from self.assertMemory(tb, 1, 0x2024)
        yield from self.assertMemory(tb, 2, 0x1200)

    @simulation_test(regs=[0x1012],
                     code=[ROT (R1, R0, 1),
                           ROT (R2, R0, 8)])
    def test_ROT(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x1012)
        yield from self.assertMemory(tb, 1, 0x2024)
        yield from self.assertMemory(tb, 2, 0x1210)

    @simulation_test(regs=[0x1234],
                     code=[MOV (R1, R0)])
    def test_MOV(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x1234)
        yield from self.assertMemory(tb, 1, 0x1234)

    @simulation_test(regs=[0x1210, 0x9210],
                     code=[SRL (R2, R0, 1),
                           SRL (R3, R0, 8),
                           SRL (R4, R1, 1),
                           SRL (R5, R1, 8)])
    def test_SRL(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x1210)
        yield from self.assertMemory(tb, 2, 0x0908)
        yield from self.assertMemory(tb, 3, 0x0012)
        yield from self.assertMemory(tb, 1, 0x9210)
        yield from self.assertMemory(tb, 4, 0x4908)
        yield from self.assertMemory(tb, 5, 0x0092)

    @simulation_test(regs=[0x1210, 0x9210],
                     code=[SRA (R2, R0, 1),
                           SRA (R3, R0, 8),
                           SRA (R4, R1, 1),
                           SRA (R5, R1, 8)])
    def test_SRA(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x1210)
        yield from self.assertMemory(tb, 2, 0x0908)
        yield from self.assertMemory(tb, 3, 0x0012)
        yield from self.assertMemory(tb, 1, 0x9210)
        yield from self.assertMemory(tb, 4, 0xC908)
        yield from self.assertMemory(tb, 5, 0xFF92)

    @simulation_test(regs=[0x0005, 0x0000, 0x0000, 0x0000,
                           0x1234, 0x5678, 0xABCD, 0x0000],
                     code=[LD  (R1, R0,  0),
                           LD  (R2, R0,  1),
                           LD  (R3, R0, -1)])
    def test_LD(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x0005)
        yield from self.assertMemory(tb, 1, 0x5678)
        yield from self.assertMemory(tb, 2, 0xABCD)
        yield from self.assertMemory(tb, 3, 0x1234)

    @simulation_test(regs=[0x0001, 0x0000],
                     code=[LDX (R1, R0, 0)],
                     extr=[0x0000, 0x1234])
    def test_LDX(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x0001)
        yield from self.assertMemory(tb, 1, 0x1234)

    @simulation_test(regs=[0x0005, 0x5678, 0xABCD, 0x1234,
                           0x0000, 0x0000, 0x0000, 0x0000],
                     code=[ST  (R1, R0,  0),
                           ST  (R2, R0,  1),
                           ST  (R3, R0, -1)])
    def test_ST(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x0005)
        yield from self.assertMemory(tb, 1, 0x5678)
        yield from self.assertMemory(tb, 2, 0xABCD)
        yield from self.assertMemory(tb, 3, 0x1234)

    @simulation_test(regs=[0x0001, 0x1234],
                     code=[STX (R1, R0, 0)],
                     extr=[0x0000, 0x0000])
    def test_STX(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x0001)
        yield from self.assertExternal(tb, 1, 0x1234)

    @simulation_test(regs=[0xabcd],
                     code=[MOVL(R0, 0x12)])
    def test_MOVL(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x0012)

    @simulation_test(regs=[0xabcd],
                     code=[MOVH(R0, 0x12)])
    def test_MOVH(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x1200)

    @simulation_test(regs=[0xabcd],
                     code=[MOVA(R0, 1)])
    def test_MOVA(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x000a)

    @simulation_test(regs=[1234, 1234],
                     code=[ADDI(R0, +42),
                           ADDI(R1, -42)])
    def test_ADDI(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 1234+42)
        yield from self.assertMemory(tb, 1, 1234-42)

    @simulation_test(regs=[0xabcd, 0xabcd],
                     code=[MOVI(R0, 0x12),
                           MOVI(R1, 0x1234),
                           MOVI(R2, 0x89ab)])
    def test_MOVI(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x0012)
        yield from self.assertMemory(tb, 1, 0x1234)
        yield from self.assertMemory(tb, 2, 0x89ab)

    @simulation_test(regs=[0x0000, 0x0000, 0x0000, 0x0000,
                           0x0000, 0x0000, 0x1234, 0x0000],
                     code=[LDI (R0, -3)])
    def test_LDI(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x1234)

    @simulation_test(regs=[0x1234, 0x0000, 0x0000, 0x0000,
                           0x0000, 0x0000, 0x0000, 0x0000],
                     code=[STI (R0, -3)])
    def test_STI(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 6, 0x1234)

    @simulation_test(code=[JAL (R0, 1),
                           MOVL(R1, 1),
                           MOVL(R2, 1)])
    def test_JAL(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x0009)
        yield from self.assertMemory(tb, 1, 0x0000)
        yield from self.assertMemory(tb, 2, 0x0001)

    @simulation_test(regs=[0x0004],
                     code=[JR  (R0, 6),
                           MOVL(R1, 1),
                           MOVL(R2, 1)])
    def test_JR(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x0004)
        yield from self.assertMemory(tb, 1, 0x0000)
        yield from self.assertMemory(tb, 2, 0x0001)

    @simulation_test(code=[J   (1), MOVL(R0, 1), MOVL(R1, 1)])
    def test_J(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 0, 0x0000)
        yield from self.assertMemory(tb, 1, 0x0001)

    @simulation_test(regs=[0x1234, 0x1234,
                           0x5678, 0x5679],
                     code=[CMP (R0, R1), JNZ (1), MOVL(R4, 1), MOVL(R5, 1),
                           CMP (R2, R3), JNZ (1), MOVL(R6, 1), MOVL(R7, 1)])
    def test_JNZ(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 4, 0x0001)
        yield from self.assertMemory(tb, 5, 0x0001)
        yield from self.assertMemory(tb, 6, 0x0000)
        yield from self.assertMemory(tb, 7, 0x0001)

    @simulation_test(regs=[0x1234, 0x1234,
                           0x5678, 0x5679],
                     code=[CMP (R0, R1), JZ  (1), MOVL(R4, 1), MOVL(R5, 1),
                           CMP (R2, R3), JZ  (1), MOVL(R6, 1), MOVL(R7, 1)])
    def test_JZ(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 4, 0x0000)
        yield from self.assertMemory(tb, 5, 0x0001)
        yield from self.assertMemory(tb, 6, 0x0001)
        yield from self.assertMemory(tb, 7, 0x0001)

    @simulation_test(regs=[0x1234, 0x7777,
                           0x0000, 0x7777],
                     code=[ADD (R0, R0, R1), JNS (1), MOVL(R4, 1), MOVL(R5, 1),
                           ADD (R2, R2, R3), JNS (1), MOVL(R6, 1), MOVL(R7, 1)])
    def test_JNS(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 4, 0x0001)
        yield from self.assertMemory(tb, 5, 0x0001)
        yield from self.assertMemory(tb, 6, 0x0000)
        yield from self.assertMemory(tb, 7, 0x0001)

    @simulation_test(regs=[0x1234, 0x7777,
                           0x0000, 0x7777],
                     code=[ADD (R0, R0, R1), JS  (1), MOVL(R4, 1), MOVL(R5, 1),
                           ADD (R2, R2, R3), JS  (1), MOVL(R6, 1), MOVL(R7, 1)])
    def test_JS(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 4, 0x0000)
        yield from self.assertMemory(tb, 5, 0x0001)
        yield from self.assertMemory(tb, 6, 0x0001)
        yield from self.assertMemory(tb, 7, 0x0001)

    @simulation_test(regs=[0x8888, 0x7fff,
                           0x8888, 0x7777],
                     code=[ADD (R0, R0, R1), JNC (1), MOVL(R4, 1), MOVL(R5, 1),
                           ADD (R2, R2, R3), JNC (1), MOVL(R6, 1), MOVL(R7, 1)])
    def test_JNC(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 4, 0x0001)
        yield from self.assertMemory(tb, 5, 0x0001)
        yield from self.assertMemory(tb, 6, 0x0000)
        yield from self.assertMemory(tb, 7, 0x0001)

    @simulation_test(regs=[0x1234, 0x1235],
                     code=[CMP (R0, R0), JUGT(1), MOVL(R2, 1), MOVL(R3, 1),
                           CMP (R1, R0), JUGT(1), MOVL(R4, 1), MOVL(R5, 1),
                           CMP (R0, R1), JUGT(1), MOVL(R6, 1), MOVL(R7, 1)])
    def test_JUGT(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 2, 0x0001)
        yield from self.assertMemory(tb, 3, 0x0001)
        yield from self.assertMemory(tb, 4, 0x0000)
        yield from self.assertMemory(tb, 5, 0x0001)
        yield from self.assertMemory(tb, 6, 0x0001)
        yield from self.assertMemory(tb, 7, 0x0001)

    @simulation_test(regs=[0x1234, 0x1235],
                     code=[CMP (R0, R0), JULE(1), MOVL(R2, 1), MOVL(R3, 1),
                           CMP (R1, R0), JULE(1), MOVL(R4, 1), MOVL(R5, 1),
                           CMP (R0, R1), JULE(1), MOVL(R6, 1), MOVL(R7, 1)])
    def test_JULE(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 2, 0x0000)
        yield from self.assertMemory(tb, 3, 0x0001)
        yield from self.assertMemory(tb, 4, 0x0001)
        yield from self.assertMemory(tb, 5, 0x0001)
        yield from self.assertMemory(tb, 6, 0x0000)
        yield from self.assertMemory(tb, 7, 0x0001)

    @simulation_test(regs=[0x0123, 0x8123],
                     code=[CMP (R0, R0), JSGE(1), MOVL(R2, 1), MOVL(R3, 1),
                           CMP (R1, R0), JSGE(1), MOVL(R4, 1), MOVL(R5, 1),
                           CMP (R0, R1), JSGE(1), MOVL(R6, 1), MOVL(R7, 1)])
    def test_JSGE(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 2, 0x0000)
        yield from self.assertMemory(tb, 3, 0x0001)
        yield from self.assertMemory(tb, 4, 0x0001)
        yield from self.assertMemory(tb, 5, 0x0001)
        yield from self.assertMemory(tb, 6, 0x0000)
        yield from self.assertMemory(tb, 7, 0x0001)

    @simulation_test(regs=[0x0123, 0x8123],
                     code=[CMP (R0, R0), JSLT(1), MOVL(R2, 1), MOVL(R3, 1),
                           CMP (R1, R0), JSLT(1), MOVL(R4, 1), MOVL(R5, 1),
                           CMP (R0, R1), JSLT(1), MOVL(R6, 1), MOVL(R7, 1)])
    def test_JSLT(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 2, 0x0001)
        yield from self.assertMemory(tb, 3, 0x0001)
        yield from self.assertMemory(tb, 4, 0x0000)
        yield from self.assertMemory(tb, 5, 0x0001)
        yield from self.assertMemory(tb, 6, 0x0001)
        yield from self.assertMemory(tb, 7, 0x0001)

    @simulation_test(regs=[0x0123, 0x8123],
                     code=[CMP (R0, R0), JSGT(1), MOVL(R2, 1), MOVL(R3, 1),
                           CMP (R1, R0), JSGT(1), MOVL(R4, 1), MOVL(R5, 1),
                           CMP (R0, R1), JSGT(1), MOVL(R6, 1), MOVL(R7, 1)])
    def test_JSGT(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 2, 0x0001)
        yield from self.assertMemory(tb, 3, 0x0001)
        yield from self.assertMemory(tb, 4, 0x0001)
        yield from self.assertMemory(tb, 5, 0x0001)
        yield from self.assertMemory(tb, 6, 0x0000)
        yield from self.assertMemory(tb, 7, 0x0001)

    @simulation_test(regs=[0x0123, 0x8123],
                     code=[CMP (R0, R0), JSLE(1), MOVL(R2, 1), MOVL(R3, 1),
                           CMP (R1, R0), JSLE(1), MOVL(R4, 1), MOVL(R5, 1),
                           CMP (R0, R1), JSLE(1), MOVL(R6, 1), MOVL(R7, 1)])
    def test_JSLE(self, tb):
        yield from self.run_core(tb)
        yield from self.assertMemory(tb, 2, 0x0000)
        yield from self.assertMemory(tb, 3, 0x0001)
        yield from self.assertMemory(tb, 4, 0x0000)
        yield from self.assertMemory(tb, 5, 0x0001)
        yield from self.assertMemory(tb, 6, 0x0001)
        yield from self.assertMemory(tb, 7, 0x0001)

# -------------------------------------------------------------------------------------------------

import argparse
from migen.fhdl import verilog


class BonelessTestbench(Module):
    def __init__(self, has_pins=False):
        self.submodules.ext_port = _StubMemoryPort("ext")

        if has_pins:
            self.pins = Signal(16)
            self.sync += [
                If(self.ext_port.adr == 0,
                    If(self.ext_port.re,
                        self.pins.eq(self.ext_port.dat_w)
                    ),
                    If(self.ext_port.we,
                        self.pins.eq(self.ext_port.dat_r)
                    )
                )
            ]

        self.specials.mem = Memory(width=16, depth=256)
        self.specials.mem_rdport = self.mem.get_port(has_re=True, mode=READ_FIRST)
        self.specials.mem_wrport = self.mem.get_port(write_capable=True)
        self.submodules.dut = BonelessCore(reset_addr=8,
            mem_rdport=self.mem_rdport,
            mem_wrport=self.mem_wrport,
            ext_port=self.ext_port)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("type", metavar="TYPE", choices=["bus", "pins"], default="bus")
    args = parser.parse_args()

    if args.type == "bus":
        tb  = BonelessTestbench()
        ios = {tb.ext_port.adr,
               tb.ext_port.re, tb.ext_port.dat_r,
               tb.ext_port.we, tb.ext_port.dat_w}

    if args.type == "pins":
        tb  = BonelessTestbench(has_pins=True)
        ios = {tb.pins}

    design = verilog.convert(tb, ios=ios, name="boneless")
    design.write("boneless.v")