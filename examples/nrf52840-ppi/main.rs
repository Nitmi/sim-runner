#![no_main]
#![no_std]

use core::arch::{asm, global_asm};
use core::panic::PanicInfo;
use core::ptr::{read_volatile, write_volatile};

global_asm!(r#"
    .syntax unified
    .cpu cortex-m4
    .thumb
    .section .vector_table,"a",%progbits
    .align 2
    .word 0x20040000
    .word Reset
    .rept 62
    .word DefaultHandler
    .endr
    .section .text.Reset,"ax",%progbits
    .global Reset
    .type Reset,%function
    .thumb_func
Reset:
    cpsid i
    bl rust_main
    b .
    .global DefaultHandler
    .type DefaultHandler,%function
    .thumb_func
DefaultHandler:
    b .
"#);

const UART: usize = 0x4000_2000;
const GPIOTE: usize = 0x4000_6000;
const TIMER: usize = 0x4000_8000;
const PPI: usize = 0x4001_f000;

unsafe fn write(base: usize, offset: usize, value: u32) {
    unsafe { write_volatile((base + offset) as *mut u32, value) };
}
unsafe fn read(base: usize, offset: usize) -> u32 {
    unsafe { read_volatile((base + offset) as *const u32) }
}
unsafe fn send(bytes: &[u8]) {
    for &byte in bytes {
        unsafe {
            write(UART, 0x11c, 0);
            write(UART, 0x51c, u32::from(byte));
            while read(UART, 0x11c) == 0 { asm!("nop", options(nomem, nostack)); }
        }
    }
}
fn hex(line: &mut [u8], offset: usize, value: u32) {
    for index in 0..8 {
        line[offset + index] = b"0123456789ABCDEF"[((value >> ((7 - index) * 4)) & 15) as usize];
    }
}
unsafe fn pins() -> u32 {
    unsafe {
        let p0 = read(0x5000_0500, 0x10);
        let p1 = read(0x5000_0800, 0x10);
        ((p0 >> 11) & 1) | (((p1 >> 3) & 1) << 1) | (((p0 >> 13) & 1) << 2)
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn rust_main() -> ! {
    unsafe {
        write(UART, 0x500, 4);
        write(UART, 0x524, 0x01d7_e000);
        write(UART, 0x008, 1);
        send(b"SIM_NRF_PPI_READY v1\r\n");
        send(concat!("SIM_NRF_PPI_BUILD ", env!("SIM_BUILD_ID"), "\r\n").as_bytes());
        #[cfg(sim_unmapped_write)]
        write_volatile(0x4000_0000 as *mut u32, 1);
        write(GPIOTE, 0x510, if cfg!(sim_wrong_polarity) { 0x10b03 } else { 0x30b03 });
        write(GPIOTE, 0x514, 0x32303);
        write(GPIOTE, 0x518, 0x30d03);
        write(TIMER, 0x510, 4);
        write(TIMER, 0x540, 1000);
        write(TIMER, 0x544, 1_000_000);
        write(PPI, 0x510, if cfg!(sim_wrong_eep) { 0x40008144 } else { 0x40008140 });
        write(PPI, 0x514, if cfg!(sim_wrong_tep) { 0x40006008 } else { 0x40006000 });
        write(PPI, 0x910, if cfg!(sim_missing_fork) { 0 } else { 0x40006004 });
        if !cfg!(sim_disabled_channel) { write(PPI, 0x504, 1); }
    }
    let mut last = 0;
    let mut sequence = 0;
    loop {
        unsafe {
            if pins() == last { continue; }
            // Functional settling window only; not an electrical debounce or timing test.
            for _ in 0..20_000 { asm!("nop", options(nomem, nostack, preserves_flags)); }
            let current = pins();
            if current == last { continue; }
            last = current;
            sequence += 1;
            let mut line = *b"SIM_NRF_PPI_SAMPLE 00000000 A=00000000 B=00000000 S=00000000\r\n";
            hex(&mut line, 19, sequence);
            hex(&mut line, 30, current & 1);
            hex(&mut line, 41, (current >> 1) & 1);
            hex(&mut line, 52, (current >> 2) & 1);
            send(&line);
        }
    }
}

#[panic_handler]
fn panic(_info: &PanicInfo<'_>) -> ! {
    loop { unsafe { asm!("nop", options(nomem, nostack)); } }
}
