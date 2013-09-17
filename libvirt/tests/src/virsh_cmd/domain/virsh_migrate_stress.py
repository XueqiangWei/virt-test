import logging
import threading
from autotest.client import utils
from autotest.client.shared import error
from virttest import libvirt_vm, data_dir, utils_misc, virt_vm, aexpect
from virttest.libvirt_xml import vm_xml

 
# To get result in thread, using global parameters
# Result of virsh migrate command
global ret_migration
# A lock for threads
global ret_lock
# True means command executed successfully
ret_migration = True
ret_lock = threading.RLock()


def cleanup_dest(vm, srcuri, desturi):
    """
    Cleanup migrated vm on remote host.
    """
    vm.connect_uri = desturi
    if vm.exists():
        if vm.is_persistent():
            vm.undefine()
        if vm.is_alive():
            vm.destroy()
    # Set connect uri back to local uri
    vm.connect_uri = srcuri


def set_cpu_memory(vm_name, cpu, memory):
    """
    Change vms' cpu and memory.
    """
    vmxml = vm_xml.VMXML.new_from_dumpxml(vm_name)
    vmxml.vcpu = cpu
    # To avoid exceeded current memory
    vmxml.max_mem = memory
    vmxml.current_mem = memory
    logging.debug("VMXML info:\n%s", vmxml.get('xml'))
    vmxml.undefine()
    vmxml.define()


class StressError(Exception):
    pass


class VMStress(object):

    """class for stress tool in vm."""

    def __init__(self, vm):
        self.vm = vm
        self.params = vm.params
        self.link = self.params.get("download_link")
        self.md5sum = self.params.get("md5sum")
        self.tmp_dir = self.params.get("tmp_dir")
        self.install_cmd = self.params.get("install_cmd") % self.tmp_dir
        self.config_cmd = self.params.get("config_cmd")
        self.start_cmd = self.params.get("start_cmd")
        self.stop_cmd = self.params.get("stop_cmd")
        self.check_cmd = self.params.get("check_cmd")
        self.app_check_cmd = self.params.get("app_check_cmd")

    def get_session(self):
        try:
            session = self.vm.wait_for_login()
            return session
        except aexpect.ShellError, detail:
            raise StressError("Login %s failed:\n%s", self.vm.name, detail)

    @error.context_aware
    def install_stress_app(self):
        error.context("install stress app in guest")
        session = self.get_session()
        installed = session.cmd_status(self.params.get("app_check_cmd")) == 0
        if installed:
            logging.debug("Stress has been installed.")
            return

        try:
            pkg = utils.unmap_url_cache(data_dir.get_download_dir(),
                                        self.link, self.md5sum)
        except Exception, detail:
            raise StressError(str(detail))
        self.vm.copy_files_to(pkg, self.tmp_dir)
        s, o = session.cmd_status_output(self.install_cmd, timeout=120)
        if s != 0:
            raise StressError("Fail to install stress app(%s)" % o)

    @error.context_aware
    def load_stress(self):
        """
        load IO/CPU/Memory stress in guest;
        """
        self.install_stress_app()
        if self.app_running():
            logging.info("Stress app is already running.")
            return
        session = self.get_session()
        error.context("launch stress app in guest", logging.info)
        session.sendline(self.start_cmd)
        logging.info("Command: %s" % self.start_cmd)
        running = utils_misc.wait_for(self.app_running, first=0.5, timeout=120)
        if not running:
            raise StressError("stress app isn't running")

    @error.context_aware
    def unload_stress(self):
        """
        stop stress app
        """
        def _unload_stress():
            session = self.get_session()
            session.sendline(self.stop_cmd)
            if not self.app_running():
                return True
            return False

        error.context("stop stress app in guest", logging.info)
        utils_misc.wait_for(_unload_stress, first=2.0,
                            text="wait stress app quit", step=1.0, timeout=120)

    def app_running(self):
        """
        check stress app really run in background;
        """
        session = self.get_session()
        status = session.cmd_status(self.check_cmd, timeout=120)
        return status == 0


def thread_func_migration(vm, desturi):
    """
    Thread for virsh migrate command.

    :param vm: A libvirt vm instance(local or remote).
    :param desturi: remote host uri.
    """
    # Judge result for main_func with a global variable.
    global ret_migration
    global ret_lock
    # Migrate the domain.
    try:
        vm.migrate(desturi, ignore_status=False, debug=True)
    except error.CmdError, detail:
        logging.error("Migration to %s failed:\n%s", desturi, detail)
        ret_lock.acquire()
        ret_migration = False
        ret_lock.release()


def do_migration(vms, desturi, load_vms, stress_type, migration_type):
    """
    Migrate vms with stress.

    :param vms: migrated vms.
    :param load_vms: provided for stress.
    """
    global ret_migration
    fail_info = []
    for vm in vms:
        if stress_type == "load_vm_booting":
            if len(load_vms):
                try:
                    if not load_vms[0].is_alive:
                        load_vms[0].start()
                except virt_vm.VMStartError:
                    fail_info.append("Start load vm %s failed." % vm.name)
                    break
            else:
                logging.warn("No load vm provided.")
        elif stress_type == "load_vms_booting":
            for load_vm in load_vms:
                try:
                    if not load_vm.is_alive:
                        load_vm.start()
                except virt_vm.VMStartError:
                    fail_info.append("Start load vm %s failed." % vm.name)
                    break
        elif stress_type == "stress_tool":
            try:
                vs = VMStress(vm)
                vs.load_stress()
            except StressError, detail:
                fail_info.append("Launch stress for %s failed." % detail)
                break

    if migration_type == "orderly":
        for vm in vms:
            migration_thread = threading.Thread(target=thread_func_migration,
                                                args=(vm, desturi))
            migration_thread.start()
            migration_thread.join(60)
            if migration_thread.isAlive():
                logging.error("Migrate %s timeout.", migration_thread)
                ret_lock.acquire()
                ret_migration = False
                ret_lock.release()

    for load_vm in load_vms:
        load_vm.destroy()

    if len(fail_info):
        logging.warning("Add stress for migration failed:%s", fail_info)
    if not ret_migration:
        raise error.TestFail()


def run_virsh_migrate_stress(test, params, env):
    """
    Test migration under stress.
    """
    vm_names = params.get("migration_vms").split()
    if len(vm_names) < 2:
        raise error.TestNAError("Provide enough vms for migration first.")

    # Migrated vms' instance
    vms = []
    for vm_name in vm_names:
        vms.append(libvirt_vm.VM(vm_name, params, test.bindir,
                                 env.get("address_cache")))

    load_vm_names = params.get("load_vms").split()
    # vms for load
    load_vms = []
    for vm_name in load_vm_names:
        load_vms.append(libvirt_vm.VM(vm_name, params, test.bindir,
                        env.get("address_cache")))

    cpu = int(params.get("vm_cpu", 1))
    memory = int(params.get("vm_memory", 1048576))
    stress_type = params.get("migration_stress_type")
    migration_type = params.get("migration_type")
    dest_uri = params.get("migrate_dest_uri", "qemu+ssh://EXAMPLE/system")

    for vm in vms:
        # Keep vm dead for edit
        if vm.is_alive():
            vm.destroy()
        set_cpu_memory(vm.name, cpu, memory)

    try:
        for vm in vms:
            vm.start()
            vm.wait_for_login()
            # TODO: recover vm if start failed?
        # TODO: set ssh-autologin automatically
        do_migration(vms, dest_uri, load_vms, stress_type, migration_type)
    finally:
        for vm in vms:
            cleanup_dest(vm, None, dest_uri)
            if vm.is_alive():
                vm.destroy()
