"""Exercise the real coordinator against a register-level inverter model."""

import ast
import asyncio
import logging
import unittest
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).parents[1] / "custom_components/solis_modbus/dispatch_control.py"
# Load the coordinator without Home Assistant's entity and scheduling dependencies.
tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
nodes = [node for node in tree.body if isinstance(node, ast.ClassDef)
         and node.name == "SolisDispatchCoordinator"]
namespace = {"asyncio": asyncio, "_LOGGER": logging.getLogger(__name__),
             "MODE": 43135, "DISCHARGE": 43129, "CHARGE": 43136,
             "RC_TIMEOUT": 43282}
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
Coordinator = namespace["SolisDispatchCoordinator"]


class Inverter:
    def __init__(self):
        self.inverter_config = SimpleNamespace(wattage_chosen=5000)
        self.registers = {43135: 0, 43129: 0, 43136: 0}
        self.writes = []

    def connected(self):
        return True

    @asynccontextmanager
    async def write_batch(self):
        yield

    async def async_write_holding_register(self, address, value):
        self.writes.append((address, value))
        self.registers[address] = value

    @property
    def output(self):
        mode = self.registers[43135]
        return (self.registers[43129] * 10 if mode == 2 else
                -self.registers[43136] * 10 if mode == 1 else None)


def immediate_async_test(method):
    """Run in-memory async calls; fail if a mock unexpectedly waits for I/O."""
    @wraps(method)
    def run(self):
        coroutine = method(self)
        try:
            coroutine.send(None)
        except StopIteration:
            return
        finally:
            coroutine.close()
        self.fail("In-memory controller test unexpectedly suspended")
    return run


class ZeroDispatchTests(unittest.TestCase):
    def setUp(self):
        self.inverter = Inverter()
        self.coord = Coordinator(None, self.inverter)

    @immediate_async_test
    async def test_reported_manual_dispatch_sequence(self):
        await self.coord.set_self_use(False)
        for target in (5000, 0, 2000, 0, -500, 0):
            with self.subTest(target=target):
                await self.coord.set_power(target)
                self.assertEqual(self.inverter.output, target)

    @immediate_async_test
    async def test_zero_uses_active_direction_and_heartbeat_preserves_hold(self):
        for target, mode, register in ((5000, 2, 43129), (-500, 1, 43136)):
            await self.coord.set_self_use(False)
            await self.coord.set_power(target)
            self.inverter.writes.clear()
            await self.coord.set_power(0)
            self.assertEqual(self.inverter.writes,
                             [(43135, mode), (register, 0), (43282, 2)])
            await self.coord._heartbeat()
            self.assertEqual(self.inverter.output, 0)

    @immediate_async_test
    async def test_staged_zero_clears_previous_discharge_when_leaving_self_use(self):
        await self.coord.set_self_use(False)
        await self.coord.set_power(5000)
        await self.coord.set_self_use(True)
        await self.coord.set_power(0)
        self.assertEqual(self.inverter.registers[43129], 500)
        await self.coord.set_self_use(False)
        self.assertEqual(self.inverter.output, 0)

    @immediate_async_test
    async def test_initial_zero_clears_existing_discharge_register(self):
        self.inverter.registers[43129] = 500
        await self.coord.set_self_use(False)
        self.assertEqual(self.inverter.output, 0)


if __name__ == "__main__":
    unittest.main()

