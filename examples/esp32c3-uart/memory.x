ENTRY(_start)
MEMORY {
  IRAM : ORIGIN = 0x40380000, LENGTH = 64K
  DRAM : ORIGIN = 0x3fca0000, LENGTH = 64K
}
SECTIONS {
  .text : { KEEP(*(.text.start)) *(.text*) *(.rodata*) } > IRAM
  .data : { *(.data*) *(.sdata*) } > DRAM
  .bss (NOLOAD) : { _sbss = .; *(.bss*) *(.sbss*) _ebss = .; } > DRAM
  _stack_top = ORIGIN(DRAM) + LENGTH(DRAM);
  /DISCARD/ : { *(.eh_frame*) }
}
