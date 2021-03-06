"""
Emulation assistance from Unicorn.
"""
import binascii
import gdb
import unicorn as U
import capstone as C
import pwndbg.arch
import pwndbg.disasm
import pwndbg.memory
import pwndbg.regs
import pwndbg.emu.emulator

# Map our internal architecture names onto Unicorn Engine's architecture types.
arch_to_UC = {
    'i386':    U.UC_ARCH_X86,
    'x86-64':  U.UC_ARCH_X86,
    'mips':    U.UC_ARCH_MIPS,
    'sparc':   U.UC_ARCH_SPARC,
    'arm':     U.UC_ARCH_ARM,
    'aarch64': U.UC_ARCH_ARM64,
    # 'powerpc': U.UC_ARCH_PPC,
}

arch_to_UC_consts = {
    'i386':    U.x86_const,
    'x86-64':  U.x86_const,
    'mips':    U.mips_const,
    'sparc':   U.sparc_const,
    'arm':     U.arm_const,
    'aarch64': U.arm64_const,
}

# Map our internal architecture names onto Unicorn Engine's architecture types.
arch_to_CS = {
    'i386':    C.CS_ARCH_X86,
    'x86-64':  C.CS_ARCH_X86,
    'mips':    C.CS_ARCH_MIPS,
    'sparc':   C.CS_ARCH_SPARC,
    'arm':     C.CS_ARCH_ARM,
    'aarch64': C.CS_ARCH_ARM64,
    # 'powerpc': C.CS_ARCH_PPC,
}


# Until Unicorn Engine provides full information about the specific instruction
# being executed for all architectures, we must rely on Capstone to provide
# that information.
arch_to_SYSCALL = {
    U.UC_ARCH_X86: [
        C.x86_const.X86_INS_SYSCALL,
        C.x86_const.X86_INS_SYSENTER,
        C.x86_const.X86_INS_SYSEXIT,
        C.x86_const.X86_INS_SYSRET,
        C.x86_const.X86_INS_IRET,
        C.x86_const.X86_INS_IRETD,
        C.x86_const.X86_INS_IRETQ,
        C.x86_const.X86_INS_INT,
        C.x86_const.X86_INS_INT1,
        C.x86_const.X86_INS_INT3,
    ],
    U.UC_ARCH_MIPS: [
        C.mips_const.MIPS_INS_SYSCALL
    ],
    U.UC_ARCH_SPARC: [
        C.sparc_const.SPARC_INS_T
    ],
    U.UC_ARCH_ARM: [
        C.arm_const.ARM_INS_SVC
    ],
    U.UC_ARCH_ARM64: [
        C.arm64_const.ARM64_INS_SVC
    ],
    U.UC_ARCH_PPC: [
        C.ppc_const.PPC_INS_SC
    ],
}

blacklisted_regs = ['ip']

'''
e = pwndbg.emu.emulator.Emulator()
e.until_jump()
'''

class Emulator(object):
    def __init__(self):
        self.arch = pwndbg.arch.current

        if self.arch not in arch_to_UC:
            raise NotImplementedError("Cannot emulate code for %s" % self.arch)

        self.consts = arch_to_UC_consts[self.arch]
        self.mode = self.get_mode()
        self.cs = C.Cs(arch_to_CS[self.arch], self.mode)

        print("# Instantiating Unicorn for %s" % self.arch)
        print("uc = U.Uc(%r, %r)" % (arch_to_UC[self.arch], self.mode))
        self.uc = U.Uc(arch_to_UC[self.arch], self.mode)
        self.regs = pwndbg.regs.current

        # Jump tracking state
        self._prev = None
        self._prevsize = None
        self._curr = None

        # Initialize the register state
        for reg in list(self.regs.misc) + list(self.regs.common) + list(self.regs.flags):
            enum = self.get_reg_enum(reg)

            if not reg:
                print("Could not set register %r" % reg)
                continue

            value = getattr(pwndbg.regs, reg)
            if value is None:
                if reg not in blacklisted_regs:
                    print("Could not set register %r" % reg)
                continue
            else:
                name = 'U.x86_const.UC_X86_REG_%s' % reg.upper()
                print("uc.reg_write(%(name)s, %(value)#x)" % locals())
            print(self.uc.reg_write(enum, value))

        # Add a hook for unmapped memory
        self.uc.hook_add(U.UC_HOOK_MEM_UNMAPPED, self.hook_mem_invalid)

        # Always stop executing as soon as there's an interrupt.
        self.uc.hook_add(U.UC_HOOK_INTR, self.hook_intr)

        # Map in the page that $pc is on
        self.map_page(self.pc)

    def __getattr__(self, name):
        reg = self.get_reg_enum(name)

        if reg:
            return self.uc.reg_read(reg)

        raise AttributeError("AttributeError: %r object has no attribute %r" % (self, name))

    def update_pc(self, pc=None):
        if pc is None:
            pc = pwndbg.regs.pc
        self.uc.reg_write(self.get_reg_enum(self.regs.pc), pc)

    def get_mode(self):
        """
        Retrieve the mode used by Capstone and Unicorn for the current
        architecture.

        This relies on the enums being the same.
        """
        arch = pwndbg.arch.current

        if arch in ('arm', 'aarch64'):
            return {0:C.CS_MODE_ARM,0x20:C.CS_MODE_THUMB}[pwndbg.regs.cpsr & 0x20]
        else:
            return {4:C.CS_MODE_32, 8:C.CS_MODE_64}[pwndbg.arch.ptrsize]

    def map_page(self, page):
        page = pwndbg.memory.page_align(page)
        size = pwndbg.memory.PAGE_SIZE

        print("# Mapping %#x-%#x" % (page, page+size))

        try:
            data = pwndbg.memory.read(page, size)
            data = bytes(data)
        except gdb.MemoryError:
            print("Could not map page %#x during emulation! [exception]" % page)
            return False

        if not data:
            print("Could not map page %#x during emulation! [no data]" % page)
            return False

        # print("uc.mem_map(%(page)#x, %(size)#x)" % locals())
        self.uc.mem_map(page, size)

        # print("# Writing %#x bytes"% len(data))
        # print("uc.mem_write(%(page)#x, %(data)r)" % locals())
        self.uc.mem_write(page, data)

        return True

    def hook_mem_invalid(self, uc, access, address, size, value, user_data):
        print("# Invalid access at %#x" % address)

        # Page-align the start address
        start = pwndbg.memory.page_align(address)
        size  = pwndbg.memory.page_size_align(address + size - start)
        stop  = start + size

        # Map each page with the permissions that we think it has.
        for page in range(start, stop, pwndbg.memory.PAGE_SIZE):
            if not self.map_page(page):
                return False

        # Demonstrate that it's mapped
        # data = binascii.hexlify(self.uc.mem_read(address, size))
        # print("# Memory is mapped: %#x --> %r" % (address, data))

        return True

    def hook_intr(self, uc, intno, user_data):
        """
        We never want to emulate through an interrupt.  Just stop.
        """
        print("Got an interrupt")
        self.uc.emu_stop()

    def get_reg_enum(self, reg):
        """
        Returns the Unicorn Emulator enum code for the named register.

        Also supports general registers like 'sp' and 'pc'.
        """
        if 'fsbase' in reg:
            import pdb
            pdb.set_trace()
            pass

        if not self.regs:
            return None

        # If we're looking for an exact register ('eax', 'ebp', 'r0') then
        # we can look those up easily.
        #
        #  'eax' ==> enum
        #
        if reg in self.regs.all:
            for reg_enum in (c for c in dir(self.consts) if c.endswith('_' + reg.upper())):
                return getattr(self.consts, reg_enum)

        # If we're looking for an abstract register which *is* accounted for,
        # we can also do an indirect lookup.
        #
        #   'pc' ==> 'eip' ==> enum
        #
        if hasattr(self.regs, reg):
            return self.get_reg_enum(getattr(self.regs, reg))

        # If we're looking for an abstract register which does not exist on
        # the RegisterSet objects, we need to do an indirect lookup.
        #
        #   'sp' ==> 'stack' ==> 'esp' ==> enum
        #
        elif reg == 'sp':
            return self.get_reg_enum(self.regs.stack)

        return None

    def emulate_with_hook(self, hook):
        ident = self.uc.hook_add(U.UC_HOOK_CODE, hook)
        try:
            self.uc.emu_start(self.pc, 0)
        finally:
            self.uc.hook_del(ident)

    jump_types = set([C.CS_GRP_CALL, C.CS_GRP_JUMP, C.CS_GRP_RET])

    def until_jump(self, pc=None):
        """
        Emulates instructions starting at the specified address until the
        program counter is set to an address which does not linearly follow
        the previously-emulated instruction.

        Arguments:
            pc(int): Address to start at.  If `None`, uses the current instruction.
            types(list,set): List of instruction groups to stop at.
                By default, it stops at all jumps, calls, and returns.

        Return:
            Returns a tuple containing the address of the jump instruction,
            and its target in the format (address, target).

            If emulation is forced to stop (e.g., because of a syscall or
            invalid memory access) then address is the instruction which
            could not be emulated through, and target will be None.

        Notes:
            This routine does not consider 'call $+5'
        """
        if pc is not None:
            self.update_pc(pc)

        # Set up the state.  Resetting this each time means that we will not ever
        # stop on the *current* instruction.
        self._prev = None
        self._prevsize = None
        self._curr = None

        # Add the single-step hook, start emulating, and remove the hook.
        self.emulate_with_hook(self.until_jump_hook_code)

        # We're done emulating
        return self._prev, self._curr

    def until_jump_hook_code(self, uc, address, size, user_data):

        print('# %#x' % address)

        # We have not emulated any instructions yet.
        if self._prev is None:
            pass

        # We have moved forward one linear instruction, no branch or the
        # branch target was the next instruction.
        elif self._prev + self._prevsize == address:
            pass

        # We have branched!  
        # The previous instruction does not immediately precede this one.
        else:
            self._curr = address
            print(hex(self._prev), hex(self._prevsize), '-->', hex(self._curr))
            self.uc.emu_stop()
            return

        self._prev = address
        self._prevsize = size

    def until_call(self, pc=None):
        addr, target = self.until_jump(pc)

        while target and C.CS_GRP_CALL not in pwndbg.disasm.one(addr).groups:
            addr, target = self.until_jump(target)

        return addr, target

    def until_syscall(self, pc=None):
        """
        Emulates instructions starting at the specified address until the program
        counter points at a syscall instruction (int 0x80, svc, etc.).
        """
        self.until_syscall_address = None
        self.emulate_with_hook(self.until_syscall_hook_code)
        return (self.until_syscall_address, None)

    def until_syscall_hook_code(self, uc, address, size, user_data):
        data = binascii.hexlify(self.uc.mem_read(address, size))
        print("# Executing instruction at %(address)#x with bytes %(data)s" % locals())
        self.until_syscall_address = address
