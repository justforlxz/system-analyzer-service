"""把 ``docs/samples/bootrec-v1`` 下的样例读成领域对象，供测试喂给写入接口。

只做「ZIP 里的 JSONL 反序列化」这一件事，不实现 1.3 的 bootrec 导入插件：不核对 manifest 的
校验和、不做 ``_BOOT_ID`` 归一化、不写库。样例由
``python -m system_analyzer.tools.make_bootrec_samples`` 生成，内容固定。
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from system_analyzer.domain.bootrec import (
    ENTITY_MODELS,
    CollectionRef,
    Manifest,
    Record,
    ResourceAvailability,
)

SAMPLE_DIR = Path(__file__).resolve().parents[2] / "docs" / "samples" / "bootrec-v1"


@dataclass(frozen=True, slots=True)
class SampleBundle:
    """一份样例采集：清单里的 Collection、实体与记录。"""

    name: str
    collection: CollectionRef
    resources: ResourceAvailability
    entities: list[BaseModel]
    records: list[Record]
    record_lines: list[dict[str, Any]]
    source_sha256: str


def load_sample(name: str) -> SampleBundle:
    path = SAMPLE_DIR / name
    with zipfile.ZipFile(path) as archive:
        manifest = Manifest.model_validate_json(archive.read("manifest.json"))
        entities: list[BaseModel] = []
        for line in archive.read("entities.jsonl").splitlines():
            if not line:
                continue
            entity_type = json.loads(line)["entity_type"]
            entities.append(ENTITY_MODELS[entity_type].model_validate_json(line))
        lines = [json.loads(line) for line in archive.read("records.jsonl").splitlines() if line]
        records = [Record.model_validate(line) for line in lines]
    return SampleBundle(
        name=name,
        collection=manifest.collection,
        resources=manifest.resources,
        entities=entities,
        records=records,
        # 原始字典：写入接口按它核对 record_id，和 1.3 导入方拿到的东西一致
        record_lines=lines,
        # 存储只要求「同一份文件的摘要相同」，摘要的具体算法属于导入方（data-model 第 8 节）
        source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
