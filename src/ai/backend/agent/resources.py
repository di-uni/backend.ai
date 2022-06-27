from __future__ import annotations

import json
import logging
from abc import ABCMeta, abstractmethod
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Collection,
    FrozenSet,
    Iterator,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Set,
    TextIO,
    Tuple,
    Type,
    cast,
)

import aiodocker
import attr

from ai.backend.common.logging import BraceStyleAdapter
from ai.backend.common.plugin import AbstractPlugin, BasePluginContext
from ai.backend.common.types import (
    BinarySize,
    DeviceId,
    DeviceModelInfo,
    DeviceName,
    HardwareMetadata,
    MountPermission,
    MountTypes,
    ResourceSlot,
    SlotName,
    SlotTypes,
)

# Expose legacy import names for plugins
from .alloc_map import AbstractAllocMap as AbstractAllocMap  # noqa: F401
from .alloc_map import AllocationStrategy as AllocationStrategy  # noqa: F401
from .alloc_map import DeviceSlotInfo as DeviceSlotInfo  # noqa: F401
from .alloc_map import (  # noqa: F401
    DiscretePropertyAllocMap as DiscretePropertyAllocMap,
)
from .alloc_map import FractionAllocMap as FractionAllocMap  # noqa: F401
from .stats import ContainerMeasurement, NodeMeasurement, StatContext
from .types import Container as SessionContainer

if TYPE_CHECKING:
    from io import TextIOWrapper

    from aiofiles.threadpool.text import AsyncTextIOWrapper

log = BraceStyleAdapter(logging.getLogger(__name__))

known_slot_types: Mapping[SlotName, SlotTypes] = {}


@attr.s(auto_attribs=True, slots=True)
class KernelResourceSpec:
    """
    This struct-like object stores the kernel resource allocation information
    with serialization and deserialization.

    It allows seamless reconstruction of allocations even when the agent restarts
    while kernel containers are running.
    """

    container_id: str
    """The container ID to refer inside containers."""

    slots: Mapping[SlotName, str]
    """Stores the original user-requested resource slots."""

    allocations: MutableMapping[DeviceName, Mapping[SlotName, Mapping[DeviceId, Decimal]]]
    """
    Represents the resource allocations for each slot (device) type and devices.
    """

    scratch_disk_size: int
    """The size of scratch disk. (not implemented yet)"""

    mounts: List['Mount'] = attr.Factory(list)
    """The mounted vfolder list."""

    def freeze(self) -> None:
        """Replace the attribute setter to make it immutable."""
        # TODO: implement
        pass

        # def _frozen_setattr(self, name, value):
        #     raise RuntimeError("tried to modify a frozen KernelResourceSpec object")

        # self.mounts = tuple(self.mounts)  # type: ignore
        # # TODO: wrap slots and allocations with frozendict?
        # setattr(self, '__setattr__', _frozen_setattr)  # <-- __setattr__ is read-only... :(

    def write_to_string(self) -> str:
        mounts_str = ','.join(map(str, self.mounts))
        slots_str = json.dumps({
            k: str(v) for k, v in self.slots.items()
        })

        resource_str = f'CID={self.container_id}\n'
        resource_str += f'SCRATCH_SIZE={BinarySize(self.scratch_disk_size):m}\n'
        resource_str += f'MOUNTS={mounts_str}\n'
        resource_str += f'SLOTS={slots_str}\n'

        for device_name, slots in self.allocations.items():
            for slot_name, per_device_alloc in slots.items():
                if not (slot_name.startswith(f'{device_name}.') or slot_name == device_name):
                    raise ValueError(f'device_name ({device_name}) must be a prefix of '
                                     f'slot_name ({slot_name})')
                pieces = []
                for dev_id, alloc in per_device_alloc.items():
                    if known_slot_types.get(slot_name, 'count') == 'bytes':
                        pieces.append(f'{dev_id}:{BinarySize(alloc):s}')
                    else:
                        pieces.append(f'{dev_id}:{alloc}')
                alloc_str = ','.join(pieces)
                resource_str += f'{slot_name.upper()}_SHARES={alloc_str}\n'

        return resource_str

    def write_to_file(self, file: TextIO) -> None:
        file.write(self.write_to_string())

    @classmethod
    def read_from_string(cls, text: str) -> 'KernelResourceSpec':
        kvpairs = {}
        for line in text.split('\n'):
            if '=' not in line:
                continue
            key, val = line.strip().split('=', maxsplit=1)
            kvpairs[key] = val
        allocations = cast(
            MutableMapping[
                DeviceName,
                MutableMapping[SlotName, Mapping[DeviceId, Decimal]],
            ],
            defaultdict(lambda: defaultdict(Decimal)),
        )
        for key, val in kvpairs.items():
            if key.endswith('_SHARES'):
                slot_name = SlotName(key[:-7].lower())
                device_name = DeviceName(slot_name.split('.')[0])
                per_device_alloc: MutableMapping[DeviceId, Decimal] = {}
                for entry in val.split(','):
                    raw_dev_id, _, raw_alloc = entry.partition(':')
                    if not raw_dev_id or not raw_alloc:
                        continue
                    dev_id = DeviceId(raw_dev_id)
                    try:
                        if known_slot_types.get(slot_name, 'count') == 'bytes':
                            alloc = Decimal(BinarySize.from_str(raw_alloc))
                        else:
                            alloc = Decimal(raw_alloc)
                    except KeyError as e:
                        log.warning('A previously launched container has '
                                    'unknown slot type: {}. Ignoring it.',
                                    e.args[0])
                        continue
                    per_device_alloc[dev_id] = alloc
                allocations[device_name][slot_name] = per_device_alloc
        mounts = [Mount.from_str(m) for m in kvpairs['MOUNTS'].split(',') if m]
        return cls(
            container_id=kvpairs.get('CID', 'unknown'),
            scratch_disk_size=BinarySize.finite_from_str(kvpairs['SCRATCH_SIZE']),
            allocations=dict(allocations),
            slots=ResourceSlot(json.loads(kvpairs['SLOTS'])),
            mounts=mounts,
        )

    @classmethod
    def read_from_file(cls, file: TextIOWrapper) -> 'KernelResourceSpec':
        text = '\n'.join(file.readlines())
        return cls.read_from_string(text)

    @classmethod
    async def aread_from_file(cls, file: AsyncTextIOWrapper) -> 'KernelResourceSpec':
        text = '\n'.join(await file.readlines())  # type: ignore
        return cls.read_from_string(text)

    def to_json_serializable_dict(self) -> Mapping[str, Any]:
        o = attr.asdict(self)
        for slot_name, alloc in o['slots'].items():
            if known_slot_types.get(slot_name, 'count') == 'bytes':
                o['slots'] = f'{BinarySize(alloc):s}'
            else:
                o['slots'] = str(alloc)
        for dev_name, dev_alloc in o['allocations'].items():
            for slot_name, per_device_alloc in dev_alloc.items():
                for dev_id, alloc in per_device_alloc.items():
                    if known_slot_types.get(slot_name, 'count') == 'bytes':
                        alloc = f'{BinarySize(alloc):s}'
                    else:
                        alloc = str(alloc)
                    o['allocations'][dev_name][slot_name][dev_id] = alloc
        o['mounts'] = list(map(str, self.mounts))
        return o

    def to_json(self) -> str:
        return json.dumps(self.to_json_serializable_dict())


@attr.s(auto_attribs=True)
class AbstractComputeDevice():
    device_id: DeviceId
    hw_location: str            # either PCI bus ID or arbitrary string
    numa_node: Optional[int]    # NUMA node ID (None if not applicable)
    memory_size: int            # bytes of available per-accelerator memory
    processing_units: int       # number of processing units (e.g., cores, SMP)


class AbstractComputePlugin(AbstractPlugin, metaclass=ABCMeta):

    key: DeviceName = DeviceName('accelerator')
    slot_types: Sequence[Tuple[SlotName, SlotTypes]]
    exclusive_slot_types: Set[str]

    @abstractmethod
    async def list_devices(self) -> Collection[AbstractComputeDevice]:
        """
        Return the list of accelerator devices, as read as physically
        on the host.
        """
        raise NotImplementedError

    @abstractmethod
    async def available_slots(self) -> Mapping[SlotName, Decimal]:
        """
        Return available slot amounts for each slot key.
        """
        raise NotImplementedError

    @abstractmethod
    def get_version(self) -> str:
        """
        Return the version string of the plugin.
        """
        raise NotImplementedError

    @abstractmethod
    async def extra_info(self) -> Mapping[str, str]:
        """
        Return extra information related to this plugin,
        such as the underlying driver version and feature flags.
        """
        return {}

    @abstractmethod
    async def gather_node_measures(self, ctx: StatContext) -> Sequence[NodeMeasurement]:
        """
        Return the system-level and device-level statistic metrics.

        It may return any number of metrics using different statistics key names in the
        returning map.
        Note that the key must not conflict with other accelerator plugins and must not
        contain dots.
        """
        raise NotImplementedError

    @abstractmethod
    async def gather_container_measures(
        self,
        ctx: StatContext,
        container_ids: Sequence[str],
    ) -> Sequence[ContainerMeasurement]:
        """
        Return the container-level statistic metrics.
        """
        raise NotImplementedError

    @abstractmethod
    async def create_alloc_map(self) -> 'AbstractAllocMap':
        """
        Create and return an allocation map for this plugin.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_hooks(self, distro: str, arch: str) -> Sequence[Path]:
        """
        Return the library hook paths used by the plugin (optional).

        :param str distro: The target Linux distribution such as "ubuntu16.04" or
                           "alpine3.8"
        :param str arch: The target CPU architecture such as "amd64"
        """
        return []

    @abstractmethod
    async def generate_docker_args(
        self,
        docker: aiodocker.docker.Docker,
        device_alloc,
    ) -> Mapping[str, Any]:
        """
        When starting a new container, generate device-specific options for the
        docker container create API as a dictionary, referring the given allocation
        map.  The agent will merge it with its own options.
        """
        return {}

    async def generate_resource_data(self, device_alloc) -> Mapping[str, str]:
        """
        Generate extra resource.txt key-value pair sets to be used by the plugin's
        own hook libraries in containers.
        """
        return {}

    @abstractmethod
    async def restore_from_container(
        self,
        container: SessionContainer,
        alloc_map: AbstractAllocMap,
    ) -> None:
        """
        When the agent restarts, retore the allocation map from the container
        metadata dictionary fetched from aiodocker.
        """
        pass

    @abstractmethod
    async def get_attached_devices(
        self,
        device_alloc: Mapping[SlotName, Mapping[DeviceId, Decimal]],
    ) -> Sequence[DeviceModelInfo]:
        """
        Make up container-attached device information with allocated device id.
        """
        return []

    async def get_node_hwinfo(self) -> HardwareMetadata:
        raise NotImplementedError


class ComputePluginContext(BasePluginContext[AbstractComputePlugin]):
    plugin_group = 'backendai_accelerator_v20'

    @classmethod
    def discover_plugins(
        cls,
        plugin_group: str,
        blocklist: set[str] = None,
    ) -> Iterator[Tuple[str, Type[AbstractComputePlugin]]]:
        scanned_plugins = [*super().discover_plugins(plugin_group, blocklist)]

        def accel_lt_intrinsic(item):
            # push back "intrinsic" plugins (if exists)
            if item[0] in ('cpu', 'mem'):
                return 0
            return -1

        scanned_plugins.sort(key=accel_lt_intrinsic)
        yield from scanned_plugins

    def attach_intrinsic_device(self, plugin: AbstractComputePlugin) -> None:
        self.plugins[plugin.key] = plugin


@attr.s(auto_attribs=True, slots=True)
class Mount:
    type: MountTypes
    source: Optional[Path]
    target: Path
    permission: MountPermission = MountPermission.READ_ONLY
    opts: Optional[Mapping[str, Any]] = None

    def __str__(self):
        return f'{self.source}:{self.target}:{self.permission.value}'

    @classmethod
    def from_str(cls, s):
        source, target, perm = s.split(':')
        source = Path(source)
        type = MountTypes.BIND
        if not source.is_absolute():
            if len(source.parts) == 1:
                source = str(source)
                type = MountTypes.VOLUME
            else:
                raise ValueError('Mount source must be an absolute path '
                                 'if it is not a volume name.',
                                 source)
        target = Path(target)
        if not target.is_absolute():
            raise ValueError('Mount target must be an absolute path.', target)
        perm = MountPermission(perm)
        return cls(type, source, target, perm, None)


def bitmask2set(mask: int) -> FrozenSet[int]:
    bpos = 0
    bset = []
    while mask > 0:
        if (mask & 1) == 1:
            bset.append(bpos)
        mask = (mask >> 1)
        bpos += 1
    return frozenset(bset)
