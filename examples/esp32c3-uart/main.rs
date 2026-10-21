#![no_main]
#![no_std]

use core::arch::{asm, global_asm};
use core::panic::PanicInfo;
use core::ptr::write_volatile;

global_asm!(r#"
    .section .text.start,"ax"
    .global _start
_start:
    csrw mie, zero
    csrw mstatus, zero
    la sp, _stack_top
    la t0, _trap
    csrw mtvec, t0
    la t0, _sbss
    la t1, _ebss
1:  bgeu t0, t1, 2f
    sw zero, 0(t0)
    addi t0, t0, 4
    j 1b
2:  call rust_main
    j .
    .balign 4
_trap:
    addi sp, sp, -16
    sw t0, 0(sp)
    sw t1, 4(sp)
    csrr t0, mcause
    li t1, 7
    bne t0, t1, 5f
    csrr t0, mepc
    lhu t1, 0(t0)
    andi t1, t1, 3
    addi t1, t1, -3
    bnez t1, 3f
    addi t0, t0, 2
3:  addi t0, t0, 2
    csrw mepc, t0
    lw t0, 0(sp)
    lw t1, 4(sp)
    addi sp, sp, 16
    mret
5:  j 5b
"#);

fn send(bytes: &[u8]) {
    for &byte in bytes {
        unsafe { write_volatile(0x6000_0000 as *mut u32, byte as u32) };
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn rust_main() -> ! {
    #[cfg(sim_unmapped_write)]
    unsafe { write_volatile(0x7000_0000 as *mut u32, 1) };
    send(b"SIM_ESP32C3_READY v1\r\n");
    send(concat!("SIM_ESP32C3_BUILD ", env!("SIM_BUILD_ID"), "\r\n").as_bytes());
    for sequence in 1u32..=5 {
        let mut line = *b"SIM_ESP32C3_HEARTBEAT 00000000\r\n";
        for index in 0..8 {
            line[22 + index] = b"0123456789ABCDEF"[((sequence >> ((7 - index) * 4)) & 15) as usize];
        }
        send(&line);
    }
    #[cfg(not(sim_hang))]
    unsafe {
        // QEMU -no-reboot turns this guest software reset into a normal exit.
        write_volatile(0x6000_8000 as *mut u32, 1 << 31);
    }
    loop { unsafe { asm!("nop", options(nomem, nostack)) }; }
}

#[panic_handler]
fn panic(_info: &PanicInfo<'_>) -> ! {
    loop { unsafe { asm!("nop", options(nomem, nostack)) }; }
}
