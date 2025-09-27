from ... import *
from . import ProgramPIC18fApplet

# -------------------------------------------------------------------------------------------------

class ProgramPIC18fAppletTestCase(GlasgowAppletV2TestCase, applet=ProgramPIC18fApplet):
    @synthesis_test
    def test_build(self):
        self.assertBuilds()

    @applet_v2_simulation_test(args=["--pgd",  "A0", "--pgc",   "A1",
                                     "--pgm", "A2", "--mclr", "A3"])
    async def test_program(self, applet, ctx):
        await applet.pic_iface._clock.set_frequency(10000)
        pgm = applet.assembly.get_pin("A2")
        mclr = applet.assembly.get_pin("A3")
        self.assertEqual(ctx.get(pgm.o), 0)
        self.assertEqual(ctx.get(mclr.o), 0)
        await applet.pic_iface.enter_low_voltage_program_mode()
        self.assertEqual(ctx.get(pgm.o), 1)
        self.assertEqual(ctx.get(mclr.o), 1)
        readback = await applet.pic_iface.write_readback(0b1101, 0x3c40)
        print(f"{0x3c40:b}")
        print(f"{readback:b}")
        self.assertEqual(readback, 0x3c40)
        result = await applet.pic_iface.read(0xf)

        await applet.pic_iface.read_device_id()
        await applet.pic_iface.exit_low_voltage_program_mode()


    # The following tests don't assert anything, they just check that nothing crashes or hangs
    # and are there so I can see the waveforms
    @applet_v2_simulation_test(args=["--pgd",  "A0", "--pgc",   "A1",
                                     "--pgm", "A2", "--mclr", "A3"])
    async def test_read_device_id(self, applet, ctx):
        await applet.pic_iface._clock.set_frequency(10000)
        await applet.pic_iface.read_device_id()


    @applet_v2_simulation_test(args=["--pgd",  "A0", "--pgc",   "A1",
                                     "--pgm", "A2", "--mclr", "A3"])
    async def test_erase(self, applet, ctx):
        await applet.pic_iface._clock.set_frequency(10000)
        await applet.pic_iface.erase()

        await applet.pic_iface.read(0b1101)

    @applet_v2_simulation_test(args=["--pgd",  "A0", "--pgc",   "A1",
                                     "--pgm", "A2", "--mclr", "A3"])
    async def test_program_line(self, applet, ctx):
        await applet.pic_iface._clock.set_frequency(10000)

        await applet.pic_iface.program_line(0, [0]*16)

    @applet_v2_hardware_test(args=["-V", "3.3", "--pgd", "A1", "--pgc",  "A2",
                                   "--pgm", "A3", "--mclr", "A0"],
                            mocks=["pic_iface._pipe", "pic_iface._clock", "pic_iface._pgm_iface", "pic_iface._mclr_iface"])
    async def test_read_config_bits_hw(self, applet):
        await applet.pic_iface._clock.set_frequency(1e6)
        await applet.pic_iface.enter_low_voltage_program_mode()

        res = await applet.pic_iface.read_memory(0x300000, 14)
        for r in res:
            print(f"{r:08b}")

        devid1, devid2 = await applet.pic_iface.read_device_id()
        print(f"{devid1:02x}")
        print(f"{devid2:02x}")

        await applet.pic_iface.exit_low_voltage_program_mode()

        self.assertEqual(res[0], 0)
        self.assertEqual(res[1], 0b00100111)
        self.assertEqual(res[3], 0b00011111)
        self.assertEqual(devid2, 0x47)

    @applet_v2_hardware_test(args=["-V", "3.3", "--pgd", "A1", "--pgc",  "A2",
                                   "--pgm", "A3", "--mclr", "A0"],
                            mocks=["pic_iface._pipe", "pic_iface._clock", "pic_iface._pgm_iface", "pic_iface._mclr_iface"])
    async def test_program_line_hw(self, applet):
        await applet.pic_iface._clock.set_frequency(1e6)
        await applet.pic_iface.enter_low_voltage_program_mode()

        await applet.pic_iface.erase()

        data = bytes(range(16))

        await applet.pic_iface.program_line(0x800, data)
        res = await applet.pic_iface.read_memory(0x800, 16)

        await applet.pic_iface.exit_low_voltage_program_mode()

        self.assertEqual(res.tobytes(), data)