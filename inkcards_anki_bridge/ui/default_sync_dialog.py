"""Anki 原生 Qt 默认同步主界面。"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from .dialog import DeviceManagementDialog, _enum_value
from .presenter import DeckItem, InkCardsPresenter, ViewState
from .settings_dialog import SettingsDialog


def _load_qt() -> Any:
    from aqt import qt  # type: ignore[import-not-found]

    return qt


def _profile_name(parent: Any) -> str:
    manager = getattr(parent, "pm", None)
    value = getattr(manager, "name", None)
    if callable(value):
        try:
            value = value()
        except Exception:
            value = None
    return value.strip() if isinstance(value, str) and value.strip() else ""


class InkCardsDialog:
    """默认同步牌组管理页；辅助设置窗口不会影响 runtime 生命周期。"""

    def __init__(
        self,
        *,
        parent: Any,
        runtime_provider: Callable[[], Any],
        config_writer: Callable[[bool], Any] | None = None,
        logging_policy_provider: Callable[[], Any] | None = None,
        logging_policy_writer: Callable[[Any], Any] | None = None,
        qt_module: Any = None,
        presenter: InkCardsPresenter | None = None,
    ) -> None:
        self._qt = qt_module or _load_qt()
        self._presenter = presenter or InkCardsPresenter(
            runtime_provider,
            config_writer=config_writer,
        )
        self._parent = parent
        self._runtime_provider = runtime_provider
        self._config_writer = config_writer
        self._logging_policy_provider = logging_policy_provider
        self._logging_policy_writer = logging_policy_writer
        self._dialog = self._qt.QDialog(parent)
        self._dialog.setWindowTitle("InkCards · 牌组同步")
        set_modal = getattr(self._dialog, "setModal", None)
        if callable(set_modal):
            set_modal(False)
        self._catalog_signature: Tuple[Tuple[Any, ...], ...] = ()
        self._tree_item_by_id: Dict[str, Any] = {}
        self._effective_excluded_deck_ids: set[str] = set()
        self._expanded_deck_ids: set[str] = set()
        self._tree_built = False
        self._updating_tree = False
        self._revealing_filter = False
        self._cleaned = False
        self._poll_count = 0
        self._device_dialog: DeviceManagementDialog | None = None
        self._settings_dialog: SettingsDialog | None = None
        self._build_widgets()
        finished = getattr(self._dialog, "finished", None)
        if finished is not None and callable(getattr(finished, "connect", None)):
            finished.connect(self._on_finished)

    @property
    def presenter(self) -> InkCardsPresenter:
        return self._presenter

    @property
    def action_parent(self) -> Any:
        return self._dialog

    @property
    def dialog(self) -> Any:
        return self._dialog

    def _build_widgets(self) -> None:
        qt = self._qt
        self._dialog.setStyleSheet(
            """
            QDialog { background: #202225; color: #f1f3f4; }
            QLabel { color: #f1f3f4; }
            QLabel#muted { color: #a9adb3; }
            QLabel#title { font-size: 25px; font-weight: 700; }
            QLabel#sectionTitle { font-size: 18px; font-weight: 650; }
            QFrame#statusCard, QFrame#sideCard {
                background: #292c30; border: 1px solid #3b3f44; border-radius: 8px;
            }
            QPushButton {
                background: #34383d; color: #f4f5f6; border: 1px solid #4b5057;
                border-radius: 6px; padding: 7px 12px;
            }
            QPushButton:hover { background: #41464d; }
            QPushButton:disabled { color: #747a81; }
            QLineEdit {
                background: #292c30; color: #f1f3f4; border: 1px solid #4b5057;
                border-radius: 6px; padding: 7px 9px;
            }
            QTreeWidget {
                background: #25282b; alternate-background-color: #2a2d31;
                color: #f1f3f4; border: 1px solid #3b3f44; border-radius: 6px;
                outline: 0;
            }
            QTreeWidget::item { min-height: 31px; border-bottom: 1px solid #363a3f; }
            QTreeWidget::item:selected { background: #36433a; }
            QHeaderView::section {
                background: #303338; color: #dfe2e5; border: 0;
                border-bottom: 1px solid #41454b; padding: 7px;
            }
            QCheckBox { color: #e9ebed; spacing: 7px; }
            """
        )
        root = qt.QVBoxLayout(self._dialog)
        margins = getattr(root, "setContentsMargins", None)
        if callable(margins):
            margins(22, 16, 22, 14)
        root.setSpacing(12)

        header = qt.QHBoxLayout()
        self._title_label = qt.QLabel("牌组同步")
        self._title_label.setObjectName("title")
        header.addWidget(self._title_label)
        profile = _profile_name(self._parent)
        self._profile_label = qt.QLabel(f"当前用户：{profile}" if profile else "")
        self._profile_label.setObjectName("muted")
        header.addWidget(self._profile_label)
        header.addStretch(1)
        self._device_management_button = qt.QPushButton("设备管理")
        self._settings_button = qt.QPushButton("设置")
        header.addWidget(self._device_management_button)
        header.addWidget(self._settings_button)
        root.addLayout(header)

        status_card = qt.QFrame()
        status_card.setObjectName("statusCard")
        status_layout = qt.QHBoxLayout(status_card)
        status_layout.setContentsMargins(14, 10, 14, 10)
        self._status_dot = qt.QLabel("●")
        self._status_headline = qt.QLabel()
        self._status_headline.setObjectName("sectionTitle")
        self._status_detail = qt.QLabel()
        self._status_detail.setObjectName("muted")
        status_text = qt.QVBoxLayout()
        status_text.addWidget(self._status_headline)
        status_text.addWidget(self._status_detail)
        status_layout.addWidget(self._status_dot)
        status_layout.addLayout(status_text, 1)
        root.addWidget(status_card)

        main = qt.QHBoxLayout()
        main.setSpacing(18)
        left = qt.QVBoxLayout()
        deck_header = qt.QHBoxLayout()
        self._deck_title = qt.QLabel("全部牌组")
        self._deck_title.setObjectName("sectionTitle")
        self._deck_count_label = qt.QLabel()
        self._deck_count_label.setObjectName("muted")
        deck_header.addWidget(self._deck_title)
        deck_header.addWidget(self._deck_count_label)
        deck_header.addStretch(1)
        left.addLayout(deck_header)
        self._deck_description = qt.QLabel(
            "默认同步全部牌组，勾选右侧「排除」即可停止同步。"
        )
        self._deck_description.setObjectName("muted")
        left.addWidget(self._deck_description)

        search_row = qt.QHBoxLayout()
        self._search = qt.QLineEdit()
        self._search.setPlaceholderText("搜索牌组…")
        self._only_excluded_checkbox = qt.QCheckBox("只看已排除")
        search_row.addWidget(self._search, 1)
        search_row.addWidget(self._only_excluded_checkbox)
        left.addLayout(search_row)

        self._tree = qt.QTreeWidget()
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(["牌组", "卡片", "同步状态", "排除"])
        self._tree.setAlternatingRowColors(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setColumnWidth(0, 340)
        self._tree.setColumnWidth(1, 90)
        self._tree.setColumnWidth(2, 110)
        self._tree.setColumnWidth(3, 75)
        left.addWidget(self._tree, 1)
        self._tree_empty_label = qt.QLabel()
        self._tree_empty_label.setObjectName("muted")
        left.addWidget(self._tree_empty_label)
        self._tree_hint = qt.QLabel(
            "新增牌组自动加入同步；排除父牌组会同时排除其子牌组。"
        )
        self._tree_hint.setObjectName("muted")
        left.addWidget(self._tree_hint)
        main.addLayout(left, 3)

        side_card = qt.QFrame()
        side_card.setObjectName("sideCard")
        side = qt.QVBoxLayout(side_card)
        side.setContentsMargins(16, 14, 16, 14)
        side.setSpacing(8)
        self._devices_title = qt.QLabel("已配对设备")
        self._devices_title.setObjectName("sectionTitle")
        side.addWidget(self._devices_title)
        self._device_summary = qt.QLabel()
        self._device_summary.setWordWrap(True)
        side.addWidget(self._device_summary)
        self._add_device_button = qt.QPushButton("添加设备")
        side.addWidget(self._add_device_button)
        self._separator = qt.QFrame()
        shape = getattr(qt.QFrame, "HLine", None)
        if shape is None:
            shape = getattr(getattr(qt.QFrame, "Shape", None), "HLine", None)
        if shape is not None:
            self._separator.setFrameShape(shape)
        side.addWidget(self._separator)
        self._recent_title = qt.QLabel("最近同步")
        self._recent_title.setObjectName("sectionTitle")
        side.addWidget(self._recent_title)
        self._recent_sync_label = qt.QLabel()
        self._recent_sync_label.setObjectName("sectionTitle")
        self._recent_sync_label.setWordWrap(True)
        side.addWidget(self._recent_sync_label)
        self._recent_sync_time_label = qt.QLabel()
        self._recent_sync_time_label.setObjectName("muted")
        side.addWidget(self._recent_sync_time_label)
        self._recent_sync_detail = qt.QLabel()
        self._recent_sync_detail.setWordWrap(True)
        side.addWidget(self._recent_sync_detail)
        side.addStretch(1)
        self._lan_notice = qt.QLabel()
        self._lan_notice.setWordWrap(True)
        self._lan_notice.setObjectName("muted")
        side.addWidget(self._lan_notice)
        self._open_settings_link = qt.QPushButton("打开设置")
        side.addWidget(self._open_settings_link)
        main.addWidget(side_card, 1)
        root.addLayout(main, 1)

        footer = qt.QHBoxLayout()
        self._policy_message = qt.QLabel()
        self._policy_message.setWordWrap(True)
        self._policy_message.setObjectName("policyMessage")
        self._footer_note = qt.QLabel(
            "排除后，设备中的副本会在下次同步后移除，Anki 中的卡片会保留。"
        )
        self._footer_note.setWordWrap(True)
        self._footer_note.setObjectName("muted")
        footer.addWidget(self._policy_message, 1)
        footer.addWidget(self._footer_note, 2)
        root.addLayout(footer)

        self._device_management_button.clicked.connect(self._open_device_management)
        self._add_device_button.clicked.connect(self._open_device_management)
        self._settings_button.clicked.connect(self._open_settings)
        self._open_settings_link.clicked.connect(self._open_settings)
        self._search.textChanged.connect(self._apply_filter)
        self._only_excluded_checkbox.toggled.connect(self._apply_filter)
        self._tree.itemChanged.connect(self._tree_item_changed)
        self._tree.itemExpanded.connect(self._remember_expansion)
        self._tree.itemCollapsed.connect(self._remember_expansion)

        self._timer = qt.QTimer(self._dialog)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._poll)
        self._render(self._presenter.view())

    def _open_device_management(self) -> None:
        if self._device_dialog is None:
            self._device_dialog = DeviceManagementDialog(
                parent=self._dialog,
                runtime_provider=self._runtime_provider,
                qt_module=self._qt,
                presenter=self._presenter,
            )
        self._device_dialog.show()

    def _open_settings(self) -> None:
        if self._settings_dialog is None:
            self._settings_dialog = SettingsDialog(
                parent=self._dialog,
                runtime_provider=self._runtime_provider,
                config_writer=self._config_writer,
                logging_policy_provider=self._logging_policy_provider,
                logging_policy_writer=self._logging_policy_writer,
                qt_module=self._qt,
                presenter=self._presenter,
            )
        self._settings_dialog.show()

    def _catalog_signature_for(
        self, decks: Iterable[DeckItem]
    ) -> Tuple[Tuple[Any, ...], ...]:
        return tuple(
            (
                item.deck_id,
                item.label,
                item.depth,
                item.parent_id,
                item.path,
                item.card_count,
                item.unsupported,
            )
            for item in decks
        )

    def _capture_expansion(self) -> None:
        self._expanded_deck_ids = {
            deck_id
            for deck_id, item in self._tree_item_by_id.items()
            if item.isExpanded()
        }

    def _remember_expansion(self, item: Any) -> None:
        if self._updating_tree or self._revealing_filter or self._search.text():
            return
        role = _enum_value(self._qt.Qt, "UserRole", 32)
        deck_id = item.data(0, role)
        if not isinstance(deck_id, str):
            return
        if item.isExpanded():
            self._expanded_deck_ids.add(deck_id)
        else:
            self._expanded_deck_ids.discard(deck_id)

    def _parent_ids(self, decks: Tuple[DeckItem, ...]) -> Dict[str, Optional[str]]:
        ids = {item.deck_id for item in decks}
        by_path = {item.path: item.deck_id for item in decks if item.path}
        result: Dict[str, Optional[str]] = {}
        for item in decks:
            parent_id = item.parent_id
            if parent_id == item.deck_id:
                parent_id = None
            if parent_id is None and len(item.path) > 1:
                parent_id = by_path.get(item.path[:-1])
            result[item.deck_id] = parent_id if parent_id in ids else None
        return result

    def _rebuild_tree(self, decks: Tuple[DeckItem, ...]) -> None:
        initial_tree = not self._tree_built
        if not initial_tree:
            self._capture_expansion()
        self._updating_tree = True
        self._tree.blockSignals(True)
        self._tree.clear()
        self._tree_item_by_id = {}
        self._tree_empty_label.setText(
            "" if decks else "正在读取牌组目录，或当前尚无牌组。"
        )
        by_id = {item.deck_id: item for item in decks}
        parent_ids = self._parent_ids(decks)
        children: Dict[str, list[str]] = {item.deck_id: [] for item in decks}
        roots = []
        for item in decks:
            parent_id = parent_ids[item.deck_id]
            if parent_id in by_id:
                children[parent_id].append(item.deck_id)
            else:
                roots.append(item.deck_id)

        order = {item.deck_id: index for index, item in enumerate(decks)}
        for child_ids in children.values():
            child_ids.sort(key=order.__getitem__)
        visited: set[str] = set()
        role = _enum_value(self._qt.Qt, "UserRole", 32)

        def append_item(deck_id: str, parent_item: Any = None) -> None:
            if deck_id in visited:
                return
            visited.add(deck_id)
            deck = by_id[deck_id]
            item = (
                self._qt.QTreeWidgetItem(parent_item)
                if parent_item is not None
                else self._qt.QTreeWidgetItem(self._tree)
            )
            item.setText(0, deck.label)
            item.setText(
                1, f"{deck.card_count:,}" if deck.card_count is not None else "—"
            )
            item.setText(2, "")
            item.setText(3, "")
            item.setData(0, role, deck.deck_id)
            if deck.unsupported:
                item.setToolTip(0, "；".join(deck.unsupported))
            self._tree_item_by_id[deck_id] = item
            for child_id in children[deck_id]:
                append_item(child_id, item)

        for root_id in roots:
            append_item(root_id)
        for deck in decks:
            if deck.deck_id not in visited:
                append_item(deck.deck_id)

        self._tree_built = True
        if initial_tree:
            self._expanded_deck_ids = {
                deck_id for deck_id in roots if children.get(deck_id)
            }
        for deck_id, item in self._tree_item_by_id.items():
            item.setExpanded(deck_id in self._expanded_deck_ids)
        self._tree.blockSignals(False)
        self._updating_tree = False

    def _update_tree_states(self, state: ViewState) -> None:
        self._updating_tree = True
        self._tree.blockSignals(True)
        effective = set(state.effective_excluded_deck_ids)
        inherited = set(state.inherited_excluded_deck_ids)
        checkable = _enum_value(self._qt.Qt, "ItemIsUserCheckable", None)
        checked = _enum_value(self._qt.Qt, "Checked", None)
        unchecked = _enum_value(self._qt.Qt, "Unchecked", None)
        for deck in state.decks:
            item = self._tree_item_by_id.get(deck.deck_id)
            if item is None:
                continue
            is_excluded = deck.deck_id in effective
            item.setText(2, "已排除" if is_excluded else "参与同步")
            item.setCheckState(3, checked if is_excluded else unchecked)
            inherited_by_parent = deck.deck_id in inherited
            flags = item.flags()
            if checkable is not None:
                flags = (
                    flags | checkable
                    if state.sync_policy_available and not inherited_by_parent
                    else flags & ~checkable
                )
                item.setFlags(flags)
            if inherited_by_parent:
                item.setToolTip(3, "已由上级牌组排除；取消父级排除后可单独设置。")
            elif not state.sync_policy_available:
                item.setToolTip(3, "排除设置暂不可用，无法确认或保存状态。")
            else:
                item.setToolTip(3, "勾选后，此牌组及其子牌组将在下次同步时排除。")
        self._tree.blockSignals(False)
        self._updating_tree = False
        self._apply_filter()

    def _tree_item_changed(self, item: Any, column: int) -> None:
        excluded_column = 3
        if self._updating_tree or column != excluded_column:
            return
        role = _enum_value(self._qt.Qt, "UserRole", 32)
        deck_id = item.data(0, role)
        checked = _enum_value(self._qt.Qt, "Checked", None)
        if isinstance(deck_id, str):
            self._presenter.set_deck_excluded(
                deck_id,
                item.checkState(excluded_column) == checked,
            )
        self._render(self._presenter.view())

    def _apply_filter(self, *_args: Any) -> None:
        if not hasattr(self, "_tree_item_by_id"):
            return
        query = self._search.text().strip().casefold()
        only_excluded = self._only_excluded_checkbox.isChecked()
        excluded = self._effective_excluded_deck_ids

        def show_if_match(item: Any) -> bool:
            role = _enum_value(self._qt.Qt, "UserRole", 32)
            deck_id = item.data(0, role)
            label_match = not query or query in item.text(0).casefold()
            own_match = label_match and (not only_excluded or deck_id in excluded)
            child_match = False
            for index in range(item.childCount()):
                child_match = show_if_match(item.child(index)) or child_match
            visible = own_match or child_match
            item.setHidden(not visible)
            return visible

        self._revealing_filter = True
        for index in range(self._tree.topLevelItemCount()):
            show_if_match(self._tree.topLevelItem(index))
        if query:
            for deck_id, item in self._tree_item_by_id.items():
                if item.childCount() and any(
                    not item.child(index).isHidden()
                    for index in range(item.childCount())
                ):
                    item.setExpanded(True)
        else:
            for deck_id, item in self._tree_item_by_id.items():
                item.setExpanded(deck_id in self._expanded_deck_ids)
        self._revealing_filter = False

    def _render(self, state: ViewState) -> None:
        overview = state.sync_overview
        self._status_headline.setText(overview.headline)
        self._status_detail.setText(overview.detail)
        self._status_dot.setText("●" if overview.service_running else "○")
        self._status_dot.setStyleSheet(
            "color: #55c86a; font-size: 22px;"
            if overview.service_running
            else "color: #a3a7ad; font-size: 22px;"
        )
        self._deck_count_label.setText(
            f"{len(state.decks)} 个牌组 · 已排除 {len(state.effective_excluded_deck_ids)} 个"
        )
        self._effective_excluded_deck_ids = set(state.effective_excluded_deck_ids)
        self._device_summary.setText(
            "\n".join(overview.device_lines)
            if overview.device_lines
            else (
                "尚未配对设备。添加设备后，在 X4 上输入配对码即可开始同步。"
                if state.sync_overview.service_running
                else "设备连接状态会在 InkCards 服务启动后显示。"
            )
        )
        self._add_device_button.setVisible(not overview.device_lines)
        self._add_device_button.setEnabled(state.buttons.add_device)
        self._recent_sync_label.setText(overview.recent_text)
        self._recent_sync_time_label.setText(overview.recent_time or "暂无同步时间")
        self._recent_sync_detail.setText(overview.detail)
        if not state.status.lan_enabled:
            self._lan_notice.setText(
                "局域网连接未启用；需要时可在设置中开启。"
                if state.status.lan_known
                else "局域网监听状态未知；请在设置中检查。"
            )
            self._open_settings_link.setVisible(True)
        else:
            self._lan_notice.setText("")
            self._open_settings_link.setVisible(False)

        if state.sync_policy_error:
            self._policy_message.setText(state.sync_policy_error)
            self._policy_message.setStyleSheet("color: #f0a29b;")
        else:
            self._policy_message.setText(
                state.sync_policy_message or "排除设置会自动保存，并在下次同步时生效。"
            )
            self._policy_message.setStyleSheet("color: #77d18a;")

        signature = self._catalog_signature_for(state.decks)
        if signature != self._catalog_signature:
            self._catalog_signature = signature
            self._rebuild_tree(state.decks)
        self._update_tree_states(state)

    def _refresh(self) -> None:
        self._render(self._presenter.refresh())

    def _poll(self) -> None:
        self._poll_count += 1
        if self._poll_count % 6 == 0:
            self._presenter.refresh_status()
            self._presenter.refresh_devices()
        self._render(self._presenter.poll())

    def _on_finished(self, _result: Any = None) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        if self._timer.isActive():
            self._timer.stop()
        if self._device_dialog is not None:
            self._device_dialog.close()
        if self._settings_dialog is not None:
            self._settings_dialog.close()
        self._presenter.close()

    def show(self) -> None:
        self._cleaned = False
        self._presenter.reopen()
        self._dialog.show()
        raise_window = getattr(self._dialog, "raise_", None)
        if callable(raise_window):
            raise_window()
        activate = getattr(self._dialog, "activateWindow", None)
        if callable(activate):
            activate()
        self._timer.start()
        self._refresh()

    def close(self) -> None:
        self._cleanup()
        self._dialog.close()

    def is_visible(self) -> bool:
        visible = getattr(self._dialog, "isVisible", None)
        return bool(visible()) if callable(visible) else False

    def dispose(self) -> None:
        self.close()
        delete_later = getattr(self._dialog, "deleteLater", None)
        if callable(delete_later):
            delete_later()


__all__ = ["InkCardsDialog"]
