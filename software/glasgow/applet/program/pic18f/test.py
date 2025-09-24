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

