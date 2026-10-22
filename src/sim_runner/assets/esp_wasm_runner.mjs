// Fixed offline host for the exact official ESP-EMU WASM distribution.
// No browser worker, WebSocket, WiFi, UART input, update or restart operation.
import fs from 'node:fs';
import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

if (process.argv.length !== 2) throw new Error('No host arguments accepted');
const read = (name) => fs.readFileSync(name);
const hash = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');
const plan = JSON.parse(read('plan.json'));
const model = JSON.parse(read('model.json'));
if (process.versions.node !== model.node_version) throw new Error('Node version differs');
if (plan.profile !== 'esp32c3-wasm-flash-v1') throw new Error('Profile differs');
for (const name of ['esp_emu.js', 'esp_emu_bg.wasm', 'package.json', 'LICENSE']) {
  if (hash(read(`component/${name}`)) !== model.files[`www/pkg/${name}`]) throw new Error('Runtime bytes differ');
}
const firmware = read('flash-input.bin');
if (hash(firmware) !== plan.firmware.sha256) throw new Error('Firmware differs');
const budget = plan.instruction_budget;
if (!Number.isSafeInteger(budget) || budget < 10000 || budget > 100000000 || budget % 10000 !== 0) throw new Error('Budget differs');
const { initSync, WasmEmulator } = await import(pathToFileURL(path.resolve('component/esp_emu.js')).href);
initSync({ module: read('component/esp_emu_bg.wasm') });
const emulator = new WasmEmulator('esp32c3');
const fd = fs.openSync('uart.bin', 'wx');
let batches = 0;
let total = 0;
try {
  if (!emulator.has_default_rom()) throw new Error('Embedded ROM unavailable');
  emulator.load_default_rom();
  emulator.set_boot_from_rom(true);
  emulator.load_firmware(firmware);
  while (batches * 10000 < budget && !emulator.needs_restart()) {
    const text = emulator.run_batch(10000);
    batches++;
    // The API returns strings, not original UART bytes. Support ASCII only.
    if (/[^\x00-\x7f]/u.test(text)) throw new Error('Non-ASCII UART string unsupported');
    const bytes = Buffer.from(text, 'utf8');
    if (total + bytes.length > plan.output_limit_bytes_per_file) throw new Error('UART output limit exceeded');
    let offset = 0;
    while (offset < bytes.length) offset += fs.writeSync(fd, bytes, offset);
    total += bytes.length;
  }
  const summary = {
    schema_version: 'embedded-sim-runner.esp-wasm-host.v1',
    node_version: process.versions.node,
    chip: 'esp32c3',
    harts: emulator.num_harts(),
    xtensa: emulator.is_xtensa(),
    batches,
    requested_instruction_upper_bound: batches * 10000,
    cycle_counter: emulator.cycles().toString(),
    cycles_per_us_claim: emulator.cycles_per_us(),
    stop_reason: emulator.needs_restart() ? 'guest_restart_requested' : 'instruction_budget',
    uart_utf8_bytes: total,
    uart_representation: 'ascii_subset_of_wasm_strings_encoded_utf8',
    restart_performed: false,
    physical_bridges: []
  };
  process.stdout.write(JSON.stringify(summary) + '\n');
} finally {
  fs.closeSync(fd);
  emulator.free();
}
