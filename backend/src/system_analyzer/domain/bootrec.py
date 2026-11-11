"""bootrec v1 容器格式的模型定义与校验。

规范正文见 ``docs/data-model.md`` 与 ``docs/bootrec-v1.md``，本模块是这两份文档的可执行
版本：字段名、必填项、缺失语义以文档为准，文档与代码不一致时先改文档再改代码。

本模块只做「读文件 + 判断是否符合规范」，不做存储、不做导入归一化：把记录写进数据库是
1.1 的事，把 ``.bootrec`` 转成内部结构是 1.3 的事。

时间字段的语义（``monotonic_ns``、``realtime_ns``、``clock_domain`` 的取值规则）由任务
0.3 定稿，这里只固定类型与单位，语义按草稿执行，文档中标注「此项待 0.3 定稿」。
"""

import hashlib
import json
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

FORMAT_NAME = "bootrec"
"""``manifest.json`` 的 ``format`` 字段固定值。"""

SUPPORTED_FORMAT_VERSION = "1.0"
"""本模块实现的格式版本。``.bootrec`` 只允许主版本号相同。"""

MANIFEST_NAME = "manifest.json"
ENTITIES_NAME = "entities.jsonl"
RECORDS_NAME = "records.jsonl"
JOURNAL_NAME = "journal.jsonl"
ARTIFACTS_PREFIX = "artifacts/"

DATA_ENTRIES = (ENTITIES_NAME, RECORDS_NAME, JOURNAL_NAME)
"""必须出现在容器里、且必须在 ``manifest.entries`` 里登记校验和的数据文件。

三者都必须存在；没有数据时写成空文件（0 行），不省略文件，避免「文件不存在」与
「文件存在但为空」两种语义并存。
"""

RECORD_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class RecordKind(StrEnum):
    """统一记录的六种数据对象。"""

    EVENT = "event"
    SPAN = "span"
    METRIC = "metric"
    PROFILE = "profile"
    ANNOTATION = "annotation"
    ARTIFACT = "artifact"


class ClockDomain(StrEnum):
    """``monotonic_ns`` 所在的时间域。此项待 0.3 定稿。"""

    BOOT_MONOTONIC = "boot_monotonic"
    WALL_REALTIME = "wall_realtime"


class Severity(StrEnum):
    """事件、注解的严重级别。"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class CollectionMode(StrEnum):
    """一次采集的来源。"""

    SSH_COLLECTION = "ssh_collection"
    JOURNAL_IMPORT = "journal_import"
    BOOTREC_IMPORT = "bootrec_import"


class EntityType(StrEnum):
    """``entities.jsonl`` 里出现的实体类型。

    Machine 与 Boot 有独立的结构（见 ``MachineEntity``、``BootEntity``），其余五类共用
    ``GenericEntity``。Collection 不出现在 ``entities.jsonl``，由 ``manifest.json`` 的
    ``collection`` 段描述。
    """

    MACHINE = "machine"
    BOOT = "boot"
    SERVICE = "service"
    PROCESS = "process"
    USER_SESSION = "user_session"
    CGROUP = "cgroup"
    METRIC_STREAM = "metric_stream"


GENERIC_ENTITY_TYPES: tuple[str, ...] = (
    EntityType.SERVICE,
    EntityType.PROCESS,
    EntityType.USER_SESSION,
    EntityType.CGROUP,
)


class _Model(BaseModel):
    """所有模型的公共配置：同一主版本内新增字段不报错，读取方忽略不认识的字段。"""

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# manifest.json
# ---------------------------------------------------------------------------


class EntryDigest(_Model):
    """容器内一个数据文件的校验和与大小。"""

    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)


class Producer(_Model):
    """生成这份 ``.bootrec`` 的程序。"""

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class ResourceAvailability(_Model):
    """这份文件整体是否带资源指标。

    单个指标流是否可用由 ``entities.jsonl`` 里 ``entity_type = metric_stream`` 的
    ``available`` 字段表达；本字段只回答「整份文件有没有资源指标」。
    """

    available: bool
    reason: str | None = None


class JournalInfo(_Model):
    """是否含 Journal 数据。不含时 ``journal.jsonl`` 必须是空文件。"""

    included: bool


class CollectionRef(_Model):
    """一次采集的标识，`.bootrec` 的描述对象之一。

    Collection 实体本身由这一段描述，不再在 ``entities.jsonl`` 里重复一行。
    """

    collection_id: str = Field(min_length=1)
    machine_id: str = Field(min_length=1)
    boot_id: str = Field(min_length=1)
    mode: CollectionMode
    created_realtime_ns: int | None = Field(default=None, ge=0)


class Counts(_Model):
    """各数据文件的行数，用于交叉核对，防止文件被截断。"""

    entities: int = Field(ge=0)
    records: int = Field(ge=0)
    journal_entries: int = Field(ge=0)


class Manifest(_Model):
    """``manifest.json`` 的结构。"""

    format: Literal["bootrec"]
    format_version: str = Field(min_length=1)
    collection: CollectionRef
    producer: Producer
    resources: ResourceAvailability
    journal: JournalInfo
    counts: Counts
    entries: dict[str, EntryDigest]


# ---------------------------------------------------------------------------
# entities.jsonl
# ---------------------------------------------------------------------------


class MachineEntity(_Model):
    """被诊断的机器。"""

    entity_type: Literal["machine"]
    machine_id: str = Field(min_length=1)
    hostname: str = Field(min_length=1)
    os_release: str | None = None
    kernel_version: str | None = None
    architecture: str | None = None


class BootEntity(_Model):
    """一次系统启动。"""

    entity_type: Literal["boot"]
    boot_id: str = Field(min_length=1)
    machine_id: str = Field(min_length=1)
    started_realtime_ns: int | None = Field(default=None, ge=0)
    kernel_version: str | None = None
    boot_index: int | None = Field(default=None, ge=0)


class GenericEntity(_Model):
    """服务、进程、用户会话、cgroup 这类有生命周期的实体。"""

    entity_type: Literal["service", "process", "user_session", "cgroup"]
    entity_id: str = Field(min_length=1)
    machine_id: str = Field(min_length=1)
    boot_id: str | None = None
    name: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class MetricStreamEntity(_Model):
    """一条指标流。``available = false`` 表示这条流没有数据，不得用 0 代替。"""

    entity_type: Literal["metric_stream"]
    entity_id: str = Field(min_length=1)
    machine_id: str = Field(min_length=1)
    boot_id: str | None = None
    name: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    available: bool
    unavailable_reason: str | None = None


ENTITY_MODELS: dict[str, type[BaseModel]] = {
    EntityType.MACHINE: MachineEntity,
    EntityType.BOOT: BootEntity,
    EntityType.METRIC_STREAM: MetricStreamEntity,
}
for _type in GENERIC_ENTITY_TYPES:
    ENTITY_MODELS[_type] = GenericEntity


# ---------------------------------------------------------------------------
# records.jsonl：统一记录字段 + 按 kind 区分的 payload
# ---------------------------------------------------------------------------


class Record(_Model):
    """一条统一记录。``payload`` 的结构由 ``kind`` 决定。"""

    record_id: str = Field(min_length=1)
    kind: RecordKind
    machine_id: str = Field(min_length=1)
    boot_id: str = Field(min_length=1)
    collection_id: str = Field(min_length=1)
    source_plugin: str = Field(min_length=1)
    stream_id: str = Field(min_length=1)
    monotonic_ns: int | None = Field(default=None, ge=0)
    realtime_ns: int | None = Field(default=None, ge=0)
    clock_domain: ClockDomain
    entity: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class EventPayload(_Model):
    """瞬时事件，例如某个 systemd 单元启动成功或失败。"""

    event_type: str = Field(min_length=1)
    severity: Severity = Severity.INFO
    message: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class SpanPayload(_Model):
    """持续过程。记录的 ``monotonic_ns`` 是开始时间，结束时间在 payload 里。"""

    span_type: str = Field(min_length=1)
    end_monotonic_ns: int | None = Field(default=None, ge=0)
    end_realtime_ns: int | None = Field(default=None, ge=0)
    state: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class MetricPayload(_Model):
    """一个采样值。缺数据时不写记录，也不写 0。"""

    name: str = Field(min_length=1)
    value: float
    unit: str = Field(min_length=1)


class ProfilePayload(_Model):
    """性能采样，原始数据放在 ``artifacts/`` 里，这里只放索引信息。"""

    profile_type: str = Field(min_length=1)
    artifact_path: str = Field(min_length=1)
    sample_count: int | None = Field(default=None, ge=0)
    attributes: dict[str, Any] = Field(default_factory=dict)


class AnnotationPayload(_Model):
    """分析结论。``evidence_refs`` 指向得出该结论的原始记录或原始文件。"""

    annotation_type: str = Field(min_length=1)
    analyzer: str = Field(min_length=1)
    severity: Severity = Severity.INFO
    message: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_refs: list[str] = Field(default_factory=list)


class ArtifactPayload(_Model):
    """``artifacts/`` 里一个原始文件的索引。"""

    path: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    description: str | None = None


PAYLOAD_MODELS: dict[RecordKind, type[BaseModel]] = {
    RecordKind.EVENT: EventPayload,
    RecordKind.SPAN: SpanPayload,
    RecordKind.METRIC: MetricPayload,
    RecordKind.PROFILE: ProfilePayload,
    RecordKind.ANNOTATION: AnnotationPayload,
    RecordKind.ARTIFACT: ArtifactPayload,
}


# ---------------------------------------------------------------------------
# journal.jsonl
# ---------------------------------------------------------------------------


class JournalEntry(_Model):
    """一条 Journal 日志。字段取自 journald 的导出字段，名字保持原样。"""

    boot_id: str = Field(min_length=1)
    realtime_ns: int = Field(ge=0)
    monotonic_ns: int | None = Field(default=None, ge=0)
    priority: int = Field(ge=0, le=7)
    message: str
    cursor: str | None = None
    unit: str | None = None
    identifier: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 校验报告
# ---------------------------------------------------------------------------


class IssueLevel(StrEnum):
    """问题的级别。``error`` 让校验不通过，``warning`` 只提示。"""

    ERROR = "error"
    WARNING = "warning"


@dataclass
class ParsedRecord:
    """已通过语法与内容校验的一条记录，带文件里的物理行号与解析后的 payload。"""

    line_number: int
    record: Record
    payload: BaseModel


@dataclass(frozen=True)
class Issue:
    """一条校验问题。``location`` 用 ``文件:行号`` 或字段路径定位。"""

    level: IssueLevel
    code: str
    location: str
    message: str

    def format(self) -> str:
        return f"[{self.level.value}] {self.code} {self.location} {self.message}"


@dataclass
class ValidationReport:
    """校验结果。``ok`` 为真表示没有任何 ``error``。"""

    path: Path
    issues: list[Issue] = field(default_factory=list)
    format_version: str | None = None
    entity_count: int = 0
    record_count: int = 0
    journal_entry_count: int = 0
    metric_record_count: int = 0
    resources_available: bool | None = None
    unavailable_streams: list[str] = field(default_factory=list)

    def add(self, level: IssueLevel, code: str, location: str, message: str) -> None:
        self.issues.append(Issue(level=level, code=code, location=location, message=message))

    def error(self, code: str, location: str, message: str) -> None:
        self.add(IssueLevel.ERROR, code, location, message)

    def warn(self, code: str, location: str, message: str) -> None:
        self.add(IssueLevel.WARNING, code, location, message)

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level is IssueLevel.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level is IssueLevel.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def format_summary(self) -> str:
        lines = [f"文件: {self.path}", f"格式版本: {self.format_version or '未知'}"]
        lines.append(
            f"结果: {'通过' if self.ok else '不通过'}（{len(self.errors)} 个错误，"
            f"{len(self.warnings)} 个警告）"
        )
        lines.append(
            f"计数: entities={self.entity_count} records={self.record_count} "
            f"journal_entries={self.journal_entry_count}"
        )
        if self.resources_available is None:
            lines.append("资源数据: 未知（manifest 不可读）")
        elif self.resources_available:
            lines.append(f"资源数据: 有（metric 记录 {self.metric_record_count} 条）")
        else:
            lines.append(
                "资源数据: 无（manifest 标记 resources.available=false，且没有 metric 记录）"
            )
        if self.unavailable_streams:
            lines.append(f"不可用指标流: {', '.join(self.unavailable_streams)}")
        else:
            lines.append("不可用指标流: 无")
        for issue in self.issues:
            lines.append(issue.format())
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# record_id 的计算：幂等导入靠它判断重复
# ---------------------------------------------------------------------------


def canonical_json_bytes(payload: Any) -> bytes:
    """规范化序列化：键排序、无多余空白、保留非 ASCII 字符。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def compute_record_id(record: dict[str, Any]) -> str:
    """按记录内容计算 ``record_id``。

    除 ``record_id`` 之外的字段全部参与计算，所以同一份数据重复生成得到同一个 id，
    内容改一个字节 id 就变。导入方用 ``(collection_id, record_id)`` 做幂等键。
    """
    body = {key: value for key, value in record.items() if key != "record_id"}
    return hashlib.blake2b(canonical_json_bytes(body), digest_size=16).hexdigest()


def sha256_hex(data: bytes) -> str:
    """容器内文件的校验和。"""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def validate_bootrec(path: str | Path) -> ValidationReport:
    """校验一个 ``.bootrec`` 文件，返回报告而不是抛异常。

    调用方看 ``report.ok`` 决定通过与否，看 ``report.issues`` 得到每条问题的位置。
    """
    report = ValidationReport(path=Path(path))
    try:
        archive = zipfile.ZipFile(report.path)
    except FileNotFoundError:
        report.error("E_FILE_NOT_FOUND", str(report.path), "文件不存在")
        return report
    except zipfile.BadZipFile:
        report.error("E_NOT_A_ZIP", str(report.path), "不是 ZIP 容器")
        return report
    except OSError as exc:
        # 目录、无读权限等情况也返回报告：校验器对「读不了的文件」与「坏文件」一样处理，
        # 不向上抛异常，调用方只看 report.ok 就够了。
        report.error("E_FILE_UNREADABLE", str(report.path), f"文件无法读取：{exc}")
        return report

    with archive:
        names = archive.namelist()
        duplicates = sorted({name for name in names if names.count(name) > 1})
        for name in duplicates:
            report.error("E_DUPLICATE_ENTRY", name, "容器内出现同名条目，读取方无法确定用哪一份")
        if MANIFEST_NAME not in names:
            report.error("E_MISSING_ENTRY", MANIFEST_NAME, "容器缺少 manifest.json")
            return report

        manifest = _read_manifest(archive, report)
        if manifest is None:
            return report
        report.format_version = manifest.format_version
        report.resources_available = manifest.resources.available
        if not _check_format_version(manifest, report):
            return report

        blobs = _verify_data_entries(archive, manifest, report)
        if blobs is None:
            return report

        entities = _read_entities(blobs[ENTITIES_NAME], report)
        records = _read_records(blobs[RECORDS_NAME], report)
        journal = _read_journal(blobs[JOURNAL_NAME], report)

        # 计数按文件里的非空行算，而不是按「解析成功的记录数」算：
        # counts 的用途是发现文件被截断，不能因为个别记录解析失败就掩盖截断。
        report.entity_count = count_jsonl_lines(blobs[ENTITIES_NAME])
        report.record_count = count_jsonl_lines(blobs[RECORDS_NAME])
        report.journal_entry_count = count_jsonl_lines(blobs[JOURNAL_NAME])

        _check_counts(manifest, report)
        entity_index = EntityIndex.build(entities, manifest, report)
        _check_records(records, manifest, entity_index, archive, report)
        _check_journal(journal, manifest, entities, report)
        _check_resources(manifest, records, entity_index, report)
        _check_unreferenced_artifacts(archive, records, report)

    return report


def _read_entry(archive: zipfile.ZipFile, name: str, report: ValidationReport) -> bytes | None:
    """读容器内一个条目的字节，读不出来时记一条错误而不是抛异常。"""
    try:
        return archive.read(name)
    except (
        zipfile.BadZipFile,
        NotImplementedError,
        RuntimeError,
        ValueError,
        OSError,
    ) as exc:
        report.error("E_ENTRY_UNREADABLE", name, f"条目无法读取：{exc}")
        return None


def _read_manifest(archive: zipfile.ZipFile, report: ValidationReport) -> Manifest | None:
    blob = _read_entry(archive, MANIFEST_NAME, report)
    if blob is None:
        return None
    try:
        payload = json.loads(blob)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        report.error("E_MANIFEST_JSON", MANIFEST_NAME, f"不是合法的 UTF-8 JSON：{exc}")
        return None
    try:
        return Manifest.model_validate(payload)
    except ValidationError as exc:
        for detail in exc.errors():
            location = ".".join(str(part) for part in detail["loc"]) or MANIFEST_NAME
            report.error("E_MANIFEST_FIELD", _manifest_field(location), detail["msg"])
        return None


def _manifest_field(location: str) -> str:
    """把 manifest 的字段路径写成 ``manifest.json › <字段>``，方便在评论里定位。"""
    return f"{MANIFEST_NAME} › {location}"


def _check_format_version(manifest: Manifest, report: ValidationReport) -> bool:
    if manifest.format != FORMAT_NAME:
        report.error("E_FORMAT_NAME", MANIFEST_NAME, f"format 必须是 {FORMAT_NAME}")
        return False
    supported = SUPPORTED_FORMAT_VERSION.split(".")
    found = manifest.format_version.split(".")
    if found[0] != supported[0]:
        report.error(
            "E_UNSUPPORTED_VERSION",
            MANIFEST_NAME,
            f"格式主版本 {found[0]} 不受支持，本实现只读 {supported[0]}.x；"
            "不允许降级猜测，请升级读取方或重新导出",
        )
        return False
    if found[1:] > supported[1:]:
        report.warn(
            "E_NEWER_MINOR_VERSION",
            MANIFEST_NAME,
            f"文件是 {manifest.format_version}，本实现按 {SUPPORTED_FORMAT_VERSION} 读取，"
            "新增字段会被忽略",
        )
    return True


def _verify_data_entries(
    archive: zipfile.ZipFile, manifest: Manifest, report: ValidationReport
) -> dict[str, bytes] | None:
    """校验三个数据文件的大小与校验和。全部通过才返回内容。"""
    names = set(archive.namelist())
    ok = True
    blobs: dict[str, bytes] = {}
    for entry in DATA_ENTRIES:
        if entry not in names:
            report.error("E_MISSING_ENTRY", entry, "容器缺少这个数据文件（没有数据时写成空文件）")
            ok = False
            continue
        digest = manifest.entries.get(entry)
        if digest is None:
            report.error("E_MISSING_CHECKSUM", f"{MANIFEST_NAME} › entries.{entry}", "缺少校验和")
            ok = False
            continue
        blob = _read_entry(archive, entry, report)
        if blob is None:
            ok = False
            continue
        if len(blob) != digest.size_bytes:
            report.error(
                "E_SIZE_MISMATCH",
                entry,
                f"实际 {len(blob)} 字节，manifest 记录 {digest.size_bytes} 字节",
            )
            ok = False
        if sha256_hex(blob) != digest.sha256:
            report.error("E_SHA256_MISMATCH", entry, "sha256 与 manifest 记录不一致，文件被改动过")
            ok = False
        blobs[entry] = blob
    for extra in sorted(set(manifest.entries) - set(DATA_ENTRIES)):
        report.warn(
            "E_UNKNOWN_CHECKSUM", f"{MANIFEST_NAME} › entries.{extra}", "不是规范中的数据文件"
        )
    return blobs if ok else None


def jsonl_lines(blob: bytes) -> list[tuple[int, bytes]]:
    """按 ``\\n`` 切 JSONL 的行，返回 ``(物理行号, 去掉行尾 \\r 的行)``，跳过空行。

    不能用 ``str.splitlines()``：它还会在 U+2028、U+2029、U+0085、``\\x0b``、``\\x0c``、
    ``\\x1c``-``\\x1e`` 这些字符处切开。JSON 字符串里允许出现这些字符（Journal 的
    ``message`` 就是任意程序的内核日志原文），用 ``splitlines()`` 会把一条记录切成两段，
    把合法文件判成非法。
    """
    lines: list[tuple[int, bytes]] = []
    for number, raw in enumerate(blob.split(b"\n"), start=1):
        line = raw.rstrip(b"\r")
        if line.strip():
            lines.append((number, line))
    return lines


def count_jsonl_lines(blob: bytes) -> int:
    """JSONL 的非空行数，与 ``jsonl_lines`` 用同一套切行规则。"""
    return len(jsonl_lines(blob))


def _iter_jsonl(blob: bytes, name: str, report: ValidationReport) -> Iterable[tuple[int, Any]]:
    for number, raw in jsonl_lines(blob):
        location = f"{name}:{number}"
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            report.error("E_JSONL_ENCODING", location, f"不是合法的 UTF-8 字节：{exc}")
            continue
        try:
            yield number, json.loads(text)
        except json.JSONDecodeError as exc:
            report.error("E_JSONL_SYNTAX", location, f"不是合法 JSON：{exc}")


def _read_entities(blob: bytes, report: ValidationReport) -> list[BaseModel]:
    entities: list[BaseModel] = []
    for number, payload in _iter_jsonl(blob, ENTITIES_NAME, report):
        location = f"{ENTITIES_NAME}:{number}"
        if not isinstance(payload, dict):
            report.error("E_ENTITY_SYNTAX", location, "每一行必须是一个 JSON 对象")
            continue
        entity_type = payload.get("entity_type")
        model = ENTITY_MODELS.get(entity_type) if isinstance(entity_type, str) else None
        if model is None:
            report.error(
                "E_ENTITY_TYPE",
                location,
                f"entity_type 必须是 {', '.join(ENTITY_MODELS)} 之一，实际是 {entity_type!r}",
            )
            continue
        try:
            entities.append(model.model_validate(payload))
        except ValidationError as exc:
            for detail in exc.errors():
                path = ".".join(str(part) for part in detail["loc"])
                report.error("E_ENTITY_FIELD", f"{location} › {path}", detail["msg"])
    return entities


def _read_records(blob: bytes, report: ValidationReport) -> list[ParsedRecord]:
    records: list[ParsedRecord] = []
    for number, payload in _iter_jsonl(blob, RECORDS_NAME, report):
        location = f"{RECORDS_NAME}:{number}"
        if not isinstance(payload, dict):
            report.error("E_RECORD_SYNTAX", location, "每一行必须是一个 JSON 对象")
            continue
        try:
            record = Record.model_validate(payload)
        except ValidationError as exc:
            for detail in exc.errors():
                path = ".".join(str(part) for part in detail["loc"])
                report.error("E_RECORD_FIELD", f"{location} › {path}", detail["msg"])
            continue
        # 格式检查放在内容检查之前：格式不对时内容摘要没有意义，两个错误码也各有分工
        if not RECORD_ID_PATTERN.fullmatch(record.record_id):
            report.error("E_RECORD_ID_FORMAT", location, "record_id 必须是 32 位小写十六进制")
            continue
        payload_model = PAYLOAD_MODELS[record.kind]
        try:
            parsed_payload = payload_model.model_validate(record.payload)
        except ValidationError as exc:
            for detail in exc.errors():
                path = ".".join(str(part) for part in detail["loc"])
                report.error("E_PAYLOAD_FIELD", f"{location} › payload.{path}", detail["msg"])
            continue
        expected = compute_record_id(payload)
        if record.record_id != expected:
            report.error(
                "E_RECORD_ID_MISMATCH",
                location,
                f"record_id 是 {record.record_id}，按内容算应为 {expected}；"
                "记录被改动过或生成方式不符合规范",
            )
            continue
        records.append(ParsedRecord(line_number=number, record=record, payload=parsed_payload))
    return records


def _read_journal(blob: bytes, report: ValidationReport) -> list[JournalEntry]:
    entries: list[JournalEntry] = []
    for number, payload in _iter_jsonl(blob, JOURNAL_NAME, report):
        location = f"{JOURNAL_NAME}:{number}"
        if not isinstance(payload, dict):
            report.error("E_JOURNAL_SYNTAX", location, "每一行必须是一个 JSON 对象")
            continue
        try:
            entries.append(JournalEntry.model_validate(payload))
        except ValidationError as exc:
            for detail in exc.errors():
                path = ".".join(str(part) for part in detail["loc"])
                report.error("E_JOURNAL_FIELD", f"{location} › {path}", detail["msg"])
    return entries


def _check_counts(manifest: Manifest, report: ValidationReport) -> None:
    actual = {
        "entities": report.entity_count,
        "records": report.record_count,
        "journal_entries": report.journal_entry_count,
    }
    for key, value in actual.items():
        declared = getattr(manifest.counts, key)
        if declared != value:
            report.error(
                "E_COUNT_MISMATCH",
                f"{MANIFEST_NAME} › counts.{key}",
                f"manifest 记录 {declared}，实际 {value}",
            )


@dataclass
class EntityIndex:
    """按各种关联键索引的实体，供交叉核对使用。"""

    machine_ids: set[str] = field(default_factory=set)
    boot_ids: set[str] = field(default_factory=set)
    by_entity_id: dict[str, BaseModel] = field(default_factory=dict)

    @classmethod
    def build(
        cls, entities: list[BaseModel], manifest: Manifest, report: ValidationReport
    ) -> "EntityIndex":
        index = cls()
        collection = manifest.collection
        for entity in entities:
            if isinstance(entity, MachineEntity):
                if entity.machine_id in index.machine_ids:
                    report.error("E_DUPLICATE_ENTITY", entity.machine_id, "同一台机器出现两行")
                index.machine_ids.add(entity.machine_id)
                if entity.machine_id != collection.machine_id:
                    report.error(
                        "E_ENTITY_MISMATCH",
                        entity.machine_id,
                        f"与 manifest 的 machine_id {collection.machine_id} 不一致",
                    )
            elif isinstance(entity, BootEntity):
                if entity.boot_id in index.boot_ids:
                    report.error("E_DUPLICATE_ENTITY", entity.boot_id, "同一次启动出现两行")
                index.boot_ids.add(entity.boot_id)
                if entity.boot_id != collection.boot_id:
                    report.error(
                        "E_ENTITY_MISMATCH",
                        entity.boot_id,
                        f"与 manifest 的 boot_id {collection.boot_id} 不一致",
                    )
                if entity.machine_id not in index.machine_ids:
                    report.error(
                        "E_DANGLING_REF",
                        entity.boot_id,
                        f"machine_id {entity.machine_id} 在 entities.jsonl 里没有对应机器",
                    )
            else:
                entity_id = entity.entity_id
                if entity_id in index.by_entity_id:
                    report.error("E_DUPLICATE_ENTITY", entity_id, "同一个 entity_id 出现两行")
                index.by_entity_id[entity_id] = entity
                if entity.machine_id not in index.machine_ids:
                    report.error(
                        "E_DANGLING_REF",
                        entity_id,
                        f"machine_id {entity.machine_id} 在 entities.jsonl 里没有对应机器",
                    )
                if entity.boot_id is not None and entity.boot_id not in index.boot_ids:
                    report.error(
                        "E_DANGLING_REF",
                        entity_id,
                        f"boot_id {entity.boot_id} 在 entities.jsonl 里没有对应启动",
                    )
                if isinstance(entity, MetricStreamEntity):
                    if entity.available and entity.unavailable_reason:
                        report.error(
                            "E_STREAM_REASON",
                            entity_id,
                            "指标流标记为可用，却写了不可用原因",
                        )
                    if not entity.available and not entity.unavailable_reason:
                        report.error(
                            "E_STREAM_REASON",
                            entity_id,
                            "指标流标记为不可用，必须写明不可用原因",
                        )
        if collection.machine_id not in index.machine_ids:
            report.error("E_MISSING_ENTITY", collection.machine_id, "缺少这台机器的 machine 行")
        if collection.boot_id not in index.boot_ids:
            report.error("E_MISSING_ENTITY", collection.boot_id, "缺少这次启动的 boot 行")
        return index


def _check_records(
    records: list[ParsedRecord],
    manifest: Manifest,
    entity_index: EntityIndex,
    archive: zipfile.ZipFile,
    report: ValidationReport,
) -> None:
    collection = manifest.collection
    seen_ids: dict[str, int] = {}
    names = set(archive.namelist())
    for parsed in records:
        record = parsed.record
        location = f"{RECORDS_NAME}:{parsed.line_number} record_id={record.record_id}"
        seen_ids[record.record_id] = seen_ids.get(record.record_id, 0) + 1
        if record.monotonic_ns is None and record.realtime_ns is None:
            report.error("E_NO_TIMESTAMP", location, "monotonic_ns 与 realtime_ns 至少要有一个")
        if record.collection_id != collection.collection_id:
            report.error("E_RECORD_SCOPE", location, "collection_id 与 manifest 不一致")
        if record.machine_id != collection.machine_id:
            report.error("E_RECORD_SCOPE", location, "machine_id 与 manifest 不一致")
        if record.boot_id != collection.boot_id:
            report.error("E_RECORD_SCOPE", location, "boot_id 与 manifest 不一致")
        _check_record_payload(parsed.payload, archive, names, report, location)
        if record.entity is None:
            if record.kind in (RecordKind.METRIC, RecordKind.SPAN):
                report.error("E_MISSING_ENTITY_REF", location, f"{record.kind} 记录必须关联实体")
            continue
        entity = entity_index.by_entity_id.get(record.entity)
        if entity is None:
            report.error(
                "E_DANGLING_REF", location, f"entity {record.entity} 在 entities.jsonl 里不存在"
            )
            continue
        if record.kind is RecordKind.METRIC and not isinstance(entity, MetricStreamEntity):
            report.error(
                "E_ENTITY_TYPE_MISMATCH", location, "metric 记录必须指向 metric_stream 实体"
            )
    for record_id, count in sorted(seen_ids.items()):
        if count > 1:
            report.error(
                "E_DUPLICATE_RECORD",
                f"{RECORDS_NAME} record_id={record_id}",
                f"同一个 record_id 出现 {count} 次，幂等键不再唯一",
            )


def _check_record_payload(
    payload: BaseModel,
    archive: zipfile.ZipFile,
    names: set[str],
    report: ValidationReport,
    location: str,
) -> None:
    """payload 里指向容器文件的字段，逐个核对文件是否真的在。"""
    if isinstance(payload, ArtifactPayload):
        _check_artifact(payload, archive, names, report, location)
    elif isinstance(payload, ProfilePayload):
        _check_artifact_path(payload.artifact_path, names, report, location)


def _check_artifact(
    payload: ArtifactPayload,
    archive: zipfile.ZipFile,
    names: set[str],
    report: ValidationReport,
    location: str,
) -> None:
    if not SHA256_PATTERN.fullmatch(payload.sha256):
        report.error("E_ARTIFACT_SHA256", location, "artifact 的 sha256 必须是 64 位小写十六进制")
        return
    if not _check_artifact_path(payload.path, names, report, location):
        return
    data = _read_entry(archive, payload.path, report)
    if data is None:
        return
    if len(data) != payload.size_bytes:
        report.error(
            "E_ARTIFACT_SIZE",
            location,
            f"原始文件实际 {len(data)} 字节，记录里写 {payload.size_bytes} 字节",
        )
    if sha256_hex(data) != payload.sha256:
        report.error("E_ARTIFACT_CHECKSUM", location, "原始文件的 sha256 与记录不一致")


def _check_artifact_path(
    path: str, names: set[str], report: ValidationReport, location: str
) -> bool:
    """路径合法且文件存在时返回 True。"""
    if not path.startswith(ARTIFACTS_PREFIX):
        report.error("E_ARTIFACT_PATH", location, f"原始文件必须放在 {ARTIFACTS_PREFIX} 下")
        return False
    if path not in names:
        report.error("E_ARTIFACT_MISSING", location, f"容器里没有 {path}")
        return False
    return True


def _check_journal(
    journal: list[JournalEntry],
    manifest: Manifest,
    entities: list[BaseModel],
    report: ValidationReport,
) -> None:
    if journal and not manifest.journal.included:
        report.error(
            "E_JOURNAL_FLAG",
            f"{MANIFEST_NAME} › journal.included",
            f"manifest 声明不含 Journal，但 journal.jsonl 有 {len(journal)} 条",
        )
    unit_ids = {
        entity.entity_id
        for entity in entities
        if isinstance(entity, GenericEntity) and entity.entity_type == EntityType.SERVICE
    }
    for number, entry in enumerate(journal, start=1):
        if entry.boot_id != manifest.collection.boot_id:
            report.error(
                "E_JOURNAL_SCOPE",
                f"{JOURNAL_NAME}:{number}",
                "boot_id 与 manifest 不一致；多启动的压缩包必须按 boot_id 拆成多个 .bootrec",
            )
        if entry.unit and f"{EntityType.SERVICE}:{entry.unit}" not in unit_ids:
            report.warn(
                "E_JOURNAL_UNIT",
                f"{JOURNAL_NAME}:{number}",
                f"日志提到的单元 {entry.unit} 没有对应的 service 实体",
            )


def _check_resources(
    manifest: Manifest,
    records: list[ParsedRecord],
    entity_index: EntityIndex,
    report: ValidationReport,
) -> None:
    metric_records = [parsed for parsed in records if parsed.record.kind is RecordKind.METRIC]
    report.metric_record_count = len(metric_records)
    unavailable = {
        entity_id
        for entity_id, entity in entity_index.by_entity_id.items()
        if isinstance(entity, MetricStreamEntity) and not entity.available
    }
    report.unavailable_streams = sorted(unavailable)
    if not manifest.resources.available and metric_records:
        report.error(
            "E_RESOURCES_FLAG",
            f"{MANIFEST_NAME} › resources.available",
            f"manifest 声明没有资源数据，却有 {len(metric_records)} 条 metric 记录",
        )
    for parsed in metric_records:
        record = parsed.record
        if record.entity in unavailable:
            report.error(
                "E_UNAVAILABLE_STREAM_HAS_DATA",
                f"{RECORDS_NAME}:{parsed.line_number} record_id={record.record_id}",
                f"指标流 {record.entity} 标记为不可用，却写了数据；缺失不得用 0 或占位值代替",
            )


def _check_unreferenced_artifacts(
    archive: zipfile.ZipFile, records: list[ParsedRecord], report: ValidationReport
) -> None:
    referenced = set()
    for parsed in records:
        payload = parsed.record.payload
        path = payload.get("artifact_path") or payload.get("path")
        if isinstance(path, str):
            referenced.add(path)
    for name in sorted(archive.namelist()):
        if name.startswith(ARTIFACTS_PREFIX) and not name.endswith("/") and name not in referenced:
            report.warn("E_ORPHAN_ARTIFACT", name, "容器里有这个文件，但没有任何记录引用它")
