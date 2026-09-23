"""KVM and Libvirt hypervisor operations engine.

Provides helper abstractions for interacting with the local or remote libvirt
daemon, parsing and generating libvirt domain XML configurations, and managing
virtual disk allocation and virtual network interface bridging.
"""
import logging
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

logger = logging.getLogger("KVMEngine")


class KVMDomainXMLError(Exception):
    """Raised when parsing or generating domain XML encounters malformed data."""


def generate_domain_xml(name: str, memory_mb: int, vcpus: int,
                        disk_path: str, bridge_name: str = "virbr0",
                        mac_address: Optional[str] = None) -> str:
    """Generate minimal Libvirt QEMU domain XML definition.

    Args:
        name: Name of the virtual machine domain.
        memory_mb: RAM allocation in megabytes.
        vcpus: Number of virtual CPU cores.
        disk_path: Absolute filesystem path to the disk image.
        bridge_name: Network bridge interface name (default 'virbr0').
        mac_address: Optional pre-assigned MAC address.

    Returns:
        String containing formatted Libvirt XML specification.
    """
    mac_elem = f"<mac address='{mac_address}'/>\n" if mac_address else ""
    return f"""<domain type='kvm'>
  <name>{name}</name>
  <memory unit='MiB'>{memory_mb}</memory>
  <currentMemory unit='MiB'>{memory_mb}</currentMemory>
  <vcpu placement='static'>{vcpus}</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <boot dev='hd'/>
  </os>
  <features>
    <acpi/>
    <apic/>
  </features>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='{disk_path}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <interface type='bridge'>
      <source bridge='{bridge_name}'/>
      <model type='virtio'/>
      {mac_elem}
    </interface>
    <graphics type='vnc' port='-1' autoport='yes' listen='0.0.0.0'/>
  </devices>
</domain>"""


def parse_domain_xml(xml_content: str) -> Dict[str, Any]:
    """Parse Libvirt domain XML and extract structural topology.

    Args:
        xml_content: XML string representation of the domain.

    Returns:
        Dictionary with extracted properties (name, memory_mb, vcpus, disks, interfaces).
    """
    try:
        root = ET.fromstring(xml_content)
    except Exception as e:
        raise KVMDomainXMLError(f"Failed to parse domain XML: {e}") from e

    name = root.findtext("name") or ""
    mem_elem = root.find("memory")
    mem_mb = int(mem_elem.text) if mem_elem is not None and mem_elem.text else 0
    if mem_elem is not None and mem_elem.get("unit") == "KiB":
        mem_mb //= 1024

    vcpu_elem = root.find("vcpu")
    vcpus = int(vcpu_elem.text) if vcpu_elem is not None and vcpu_elem.text else 1

    disks: List[Dict[str, str]] = []
    for disk in root.findall(".//devices/disk"):
        source = disk.find("source")
        target = disk.find("target")
        disks.append({
            "device": disk.get("device", "disk"),
            "file": source.get("file", "") if source is not None else "",
            "target": target.get("dev", "") if target is not None else "",
        })

    interfaces: List[Dict[str, str]] = []
    for iface in root.findall(".//devices/interface"):
        source = iface.find("source")
        mac = iface.find("mac")
        interfaces.append({
            "type": iface.get("type", ""),
            "bridge": source.get("bridge", "") if source is not None else "",
            "mac": mac.get("address", "") if mac is not None else "",
        })

    return {
        "name": name,
        "memory_mb": mem_mb,
        "vcpus": vcpus,
        "disks": disks,
        "interfaces": interfaces,
    }


class KVMEngine:
    """Interface for direct hypervisor interactions and libvirt abstraction."""

    def __init__(self, uri: str = "qemu:///system"):
        """Initialize KVMEngine with connection URI.

        Args:
            uri: Libvirt connection URI string (default 'qemu:///system').
        """
        self.uri = uri
        self._conn = None

    def connect(self) -> Any:
        """Establish or return active libvirt daemon connection.

        Returns:
            Libvirt connection object or None if libvirt is unavailable.
        """
        try:
            import libvirt
            if self._conn is None:
                self._conn = libvirt.open(self.uri)
            return self._conn
        except ImportError:
            logger.debug("libvirt library is not installed in current environment")
            return None
        except Exception as e:
            logger.warning("Failed to connect to libvirt at %s: %s", self.uri, e)
            return None

    def list_domains(self) -> List[Dict[str, Any]]:
        """Query active and inactive domains from libvirt daemon.

        Returns:
            List of domain metadata dictionaries.
        """
        conn = self.connect()
        if not conn:
            return []
        out = []
        try:
            for dom in conn.listAllDomains():
                out.append({
                    "id": dom.ID(),
                    "name": dom.name(),
                    "uuid": dom.UUIDString(),
                    "active": bool(dom.isActive()),
                })
        except Exception as e:
            logger.warning("Error listing domains: %s", e)
        return out

    def get_domain_info(self, name_or_uuid: str) -> Optional[Dict[str, Any]]:
        """Fetch runtime statistics and XML configuration for a specific domain.

        Args:
            name_or_uuid: Domain name or UUID string.

        Returns:
            Dictionary containing domain status and parsed XML details.
        """
        conn = self.connect()
        if not conn:
            return None
        try:
            dom = conn.lookupByName(name_or_uuid) if not len(name_or_uuid) == 36 else conn.lookupByUUIDString(name_or_uuid)
            xml_desc = dom.XMLDesc()
            info = parse_domain_xml(xml_desc)
            info["active"] = bool(dom.isActive())
            info["uuid"] = dom.UUIDString()
            return info
        except Exception as e:
            logger.warning("Error fetching domain info for %s: %s", name_or_uuid, e)
            return None

    def define_domain(self, xml_content: str) -> Optional[str]:
        """Define a new domain from XML specification.

        Args:
            xml_content: Libvirt domain XML string.

        Returns:
            Assigned domain UUID string if defined, None otherwise.
        """
        conn = self.connect()
        if not conn:
            return None
        try:
            dom = conn.defineXML(xml_content)
            return dom.UUIDString()
        except Exception as e:
            logger.error("Failed to define domain: %s", e)
            return None

    def start_domain(self, name_or_uuid: str) -> bool:
        """Start a defined domain.

        Args:
            name_or_uuid: Domain name or UUID string.

        Returns:
            True if started successfully, False otherwise.
        """
        conn = self.connect()
        if not conn:
            return False
        try:
            dom = conn.lookupByName(name_or_uuid) if len(name_or_uuid) != 36 else conn.lookupByUUIDString(name_or_uuid)
            return dom.create() == 0
        except Exception as e:
            logger.error("Failed to start domain %s: %s", name_or_uuid, e)
            return False

    def destroy_domain(self, name_or_uuid: str) -> bool:
        """Forcefully shut down an active domain.

        Args:
            name_or_uuid: Domain name or UUID string.

        Returns:
            True if destroyed successfully, False otherwise.
        """
        conn = self.connect()
        if not conn:
            return False
        try:
            dom = conn.lookupByName(name_or_uuid) if len(name_or_uuid) != 36 else conn.lookupByUUIDString(name_or_uuid)
            return dom.destroy() == 0
        except Exception as e:
            logger.error("Failed to destroy domain %s: %s", name_or_uuid, e)
            return False

    def undefine_domain(self, name_or_uuid: str) -> bool:
        """Undefine domain configuration from libvirt daemon.

        Args:
            name_or_uuid: Domain name or UUID string.

        Returns:
            True if undefined successfully, False otherwise.
        """
        conn = self.connect()
        if not conn:
            return False
        try:
            dom = conn.lookupByName(name_or_uuid) if len(name_or_uuid) != 36 else conn.lookupByUUIDString(name_or_uuid)
            return dom.undefine() == 0
        except Exception as e:
            logger.error("Failed to undefine domain %s: %s", name_or_uuid, e)
            return False
