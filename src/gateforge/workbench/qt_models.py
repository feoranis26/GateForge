from __future__ import annotations

from collections.abc import Mapping, Sequence

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel


_USER_ROLE = int(Qt.ItemDataRole.UserRole)
CANDIDATE_ID_ROLE = _USER_ROLE + 1
CHECKPOINT_DIGEST_ROLE = _USER_ROLE + 2
STAGE_NAME_ROLE = _USER_ROLE + 3
SOURCE_CANDIDATE_ROLE = _USER_ROLE + 4
PROPOSAL_FINGERPRINT_ROLE = _USER_ROLE + 5


class CompilationCandidateTreeModel(QStandardItemModel):
    def __init__(self) -> None:
        super().__init__()
        self.setHorizontalHeaderLabels(
            ("Candidate", "Status", "Stage", "Lower bound", "Expected")
        )

    def set_snapshot(self, snapshot: Mapping[str, object]) -> None:
        self.removeRows(0, self.rowCount())
        stages = _sequence(snapshot.get("stages"))
        for stage in stages:
            stage_data = _mapping(stage)
            name = _text(stage_data.get("name"))
            group = _group_item(name)
            row = [group, QStandardItem(), QStandardItem(), QStandardItem(), QStandardItem()]
            self.appendRow(row)
            for transition in _sequence(stage_data.get("transitions")):
                transition_data = _mapping(transition)
                candidate = _mapping(transition_data.get("candidate"))
                group.appendRow(
                    _candidate_row(
                        candidate,
                        status=_text(transition_data.get("status")),
                        stage=name,
                        source=_text(transition_data.get("source_candidate")),
                    )
                )

        frontier = _sequence(snapshot.get("frontier"))
        if frontier:
            group = _group_item("Current frontier")
            self.appendRow(
                [group, QStandardItem(), QStandardItem(), QStandardItem(), QStandardItem()]
            )
            for candidate in frontier:
                candidate_data = _mapping(candidate)
                group.appendRow(
                    _candidate_row(
                        candidate_data,
                        status="frontier",
                        stage=str(candidate_data.get("stage_index", "")),
                        source="",
                    )
                )

    @staticmethod
    def candidate_id(index: QModelIndex) -> str | None:
        if not index.isValid():
            return None
        first_column = index.siblingAtColumn(0)
        value = first_column.data(CANDIDATE_ID_ROLE)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def checkpoint_digest(index: QModelIndex) -> str | None:
        if not index.isValid():
            return None
        first_column = index.siblingAtColumn(0)
        value = first_column.data(CHECKPOINT_DIGEST_ROLE)
        return value if isinstance(value, str) and value else None


class ProposalTableModel(QStandardItemModel):
    def __init__(self) -> None:
        super().__init__()
        self.setHorizontalHeaderLabels(
            ("Mapper", "Provider", "Rule", "Disposition", "Expected", "Cells")
        )

    def set_snapshot(
        self,
        snapshot: Mapping[str, object],
        *,
        candidate_id: str | None = None,
    ) -> None:
        self.removeRows(0, self.rowCount())
        for stage in _sequence(snapshot.get("stages")):
            stage_data = _mapping(stage)
            stage_name = _text(stage_data.get("name"))
            for proposal in _sequence(stage_data.get("proposals")):
                proposal_data = _mapping(proposal)
                source = _text(proposal_data.get("source_candidate"))
                if candidate_id is not None and source != candidate_id:
                    continue
                cost = _mapping(proposal_data.get("cost"))
                cells = _sequence(proposal_data.get("cells"))
                row = [
                    QStandardItem(_text(proposal_data.get("mapper"))),
                    QStandardItem(_text(proposal_data.get("provider"))),
                    QStandardItem(_text(proposal_data.get("rule"))),
                    QStandardItem(_text(proposal_data.get("disposition"))),
                    QStandardItem(_number_text(cost.get("expected"))),
                    QStandardItem(str(len(cells))),
                ]
                row[0].setData(
                    _text(proposal_data.get("fingerprint")),
                    PROPOSAL_FINGERPRINT_ROLE,
                )
                row[0].setData(source, SOURCE_CANDIDATE_ROLE)
                row[0].setData(stage_name, STAGE_NAME_ROLE)
                self.appendRow(row)


class ClaimsTableModel(QStandardItemModel):
    def __init__(self) -> None:
        super().__init__()
        self.setHorizontalHeaderLabels(
            (
                "Instance",
                "Mapper",
                "Provider",
                "Prefab",
                "Implementation",
                "Packaging",
            )
        )

    def set_candidate_details(self, details: Mapping[str, object] | None) -> None:
        self.removeRows(0, self.rowCount())
        if details is None:
            return
        for claim in _sequence(details.get("claims")):
            claim_data = _mapping(claim)
            self.appendRow(
                [
                    QStandardItem(
                        f"{_text(claim_data.get('module'))}."
                        f"{_text(claim_data.get('instance'))}"
                    ),
                    QStandardItem(_text(claim_data.get("mapper"))),
                    QStandardItem(_text(claim_data.get("provider"))),
                    QStandardItem(_short_id(_text(claim_data.get("prefab")))),
                    QStandardItem(_text(claim_data.get("implementation_name"))),
                    QStandardItem(_text(claim_data.get("packaging"))),
                ]
            )


def _candidate_row(
    candidate: Mapping[str, object],
    *,
    status: str,
    stage: str,
    source: str,
) -> list[QStandardItem]:
    identifier = _text(candidate.get("identifier"))
    checkpoint = _text(candidate.get("checkpoint_digest"))
    first = QStandardItem(_short_id(identifier))
    first.setToolTip(identifier)
    first.setData(identifier, CANDIDATE_ID_ROLE)
    first.setData(checkpoint, CHECKPOINT_DIGEST_ROLE)
    first.setData(source, SOURCE_CANDIDATE_ROLE)
    first.setData(stage, STAGE_NAME_ROLE)
    return [
        first,
        QStandardItem(status),
        QStandardItem(stage),
        QStandardItem(_number_text(candidate.get("lower_bound"))),
        QStandardItem(_number_text(candidate.get("expected_cost"))),
    ]


def _group_item(label: str) -> QStandardItem:
    item = QStandardItem(label)
    item.setEditable(False)
    item.setSelectable(False)
    return item


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    return value if isinstance(value, Sequence) and not isinstance(value, str) else ()


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _number_text(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    return f"{value:g}"


def _short_id(identifier: str) -> str:
    return identifier[:12] if len(identifier) > 12 else identifier