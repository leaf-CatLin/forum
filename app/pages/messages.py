# -*- coding: utf-8 -*-
"""消息中心（``messages`` 路由）：点赞 / 评论 / 系统 / 团队消息。

对应新远端 ``/ov2/api/messages/*``：
- 四个分类独立游标分页；列表首屏加载完成后按最新一条的时间（聊天按最大 id）标记已读；
- 未读数来自 ``/messages/summary``，与 Web / 安卓端共用同一份已读状态。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QLineEdit, QTabWidget, QWidget

from .. import icons, theme, yearmode
from ..widgets import (Card, EmptyHint, PlainLabel, ScrollPage, button, hbox,
                       vbox)
from ..widgets.images import Avatar
from .base import Page

TABS = (
    ("likes", "点赞", "heart-line"),
    ("comments", "评论", "comment-line"),
    ("system", "系统", "inbox-line"),
    ("chat", "团队消息", "chat-line"),
)


def _strip(value) -> str:
    """消息正文是 HTML 富文本：详情页只做纯文本展示。"""
    import html as _html
    import re
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", _html.unescape(text)).strip()


def _to_html(text: str) -> str:
    """纯文本转存储态 HTML：转义尖括号，换行转 ``<br>``。"""
    import html as _html
    return "<p>%s</p>" % _html.escape(text).replace("\n", "<br>")


class _MessageRow(Card):
    """单条消息卡片（头像 + 标题 + 正文 + 时间，可点击跳转）。"""

    def __init__(self, title: str, body: str, when, *,
                 avatar: str = "", parent: QWidget | None = None,
                 on_open=None, mine: bool = False) -> None:
        super().__init__(parent, padding=(12, 10, 12, 10), spacing=6)
        if mine:
            self.setProperty("mine", "true")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        row = hbox(spacing=10)
        if avatar is not None:
            pic = Avatar(32)
            pic.set_url(str(avatar or ""))
            row.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        body_box = vbox(spacing=3)
        head = hbox(spacing=8)
        head.addWidget(PlainLabel(title, wrap=True), 1)
        if when:
            stamp = QLabel(yearmode.fmt_time(when))
            stamp.setProperty("muted", "true")
            head.addWidget(stamp, 0, Qt.AlignmentFlag.AlignTop)
        body_box.addLayout(head)
        if body:
            body_box.addWidget(PlainLabel(body, wrap=True, muted=True))
        row.addLayout(body_box, 1)
        self.body.addLayout(row)

        if on_open is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self._on_open = on_open

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        handler = getattr(self, "_on_open", None)
        if handler is not None and event.button() == Qt.MouseButton.LeftButton:
            handler()
        super().mouseReleaseEvent(event)


class _MessageList(QWidget):
    """分类列表：加载态 / 空态 / 游标分页 / 标记已读。"""

    PAGE_SIZE = 20

    def __init__(self, page: Page, category: str, *, chat: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.page = page
        self.category = category
        self.chat = chat
        self.cursor = ""
        self.busy = False
        self.loaded = False

        root = vbox(self, margins=(10, 10, 10, 10), spacing=8)
        self.status = QLabel("")
        self.status.setProperty("muted", "true")
        self.status.hide()
        root.addWidget(self.status)

        self.scroll = ScrollPage(self, spacing=8)
        root.addWidget(self.scroll, 1)

        self.empty = EmptyHint("暂无消息")
        self.empty.hide()
        root.addWidget(self.empty)

        self.more = button("加载更多", None, self.load_more)
        self.more.hide()
        root.addWidget(self.more, 0, Qt.AlignmentFlag.AlignHCenter)

        if chat:
            composer = hbox(spacing=6)
            self.input = QLineEdit(self)
            self.input.setPlaceholderText("给管理团队留言…（Enter 发送）")
            self.input.returnPressed.connect(self.send)
            composer.addWidget(self.input, 1)
            self.send_btn = button("发送", "primary", self.send,
                                   icon="send-line", icon_size=14)
            composer.addWidget(self.send_btn)
            root.addLayout(composer)

        self.scroll.load_more.connect(self.load_more)

    # ── 请求 ──
    def reload(self) -> None:
        self.cursor = ""
        self.loaded = False
        self.scroll.clear()
        self.load(first=True)

    def load_more(self) -> None:
        self.scroll.set_busy(True)
        self.more.setText("加载中…")
        self.more.setEnabled(False)
        self.load(first=False)

    def load(self, *, first: bool) -> None:
        if self.busy:
            return
        self.busy = True
        if first:
            self.status.setText("加载中…")
            self.status.show()
            self.empty.hide()

        if self.chat:
            fn = lambda: self.page.api.chat_messages(self.cursor, self.PAGE_SIZE)
        elif self.category == "likes":
            fn = lambda: self.page.api.message_likes(self.cursor, self.PAGE_SIZE)
        elif self.category == "comments":
            fn = lambda: self.page.api.message_comments(self.cursor, self.PAGE_SIZE)
        else:
            fn = lambda: self.page.api.message_system(self.cursor, self.PAGE_SIZE)

        self.page.run(fn, self._done, self._fail, label="消息-" + self.category)

    def _done(self, result) -> None:
        self.busy = False
        self.status.hide()
        self.more.setEnabled(True)
        self.more.setText("加载更多")
        if not result.ok:
            self._fail(result.message)
            return
        first_page = not self.cursor
        items = [item for item in result.rows("items") if isinstance(item, dict)]
        if self.chat:
            # 服务端按「最新优先」返回；聊天按时间正序展示（最新在底部），
            # 加载更早的一页时插到顶部，保持整段对话的时间顺序。
            rows = [self._row(item) for item in reversed(items)]
            if first_page:
                for row in rows:
                    self.scroll.add(row)
            else:
                box = self.scroll.layout_box()
                for offset, row in enumerate(rows):
                    box.insertWidget(offset, row)
        else:
            for item in items:
                self.scroll.add(self._row(item))
        has_more = bool(result.get("has_more"))
        self.cursor = str(result.get("next_cursor") or "")
        self.loaded = True
        self.more.setVisible(bool(has_more and self.cursor))
        self.scroll.set_load_more_enabled(bool(has_more and self.cursor))
        self.empty.setVisible(not (self.scroll.layout_box().count() > 1))
        self._mark_read(items)

    def _fail(self, message: str) -> None:
        self.busy = False
        self.more.setEnabled(True)
        self.more.setText("加载更多")
        self.status.setText(str(message or "加载失败"))
        self.status.show()

    # ── 已读 ──
    def _mark_read(self, items: list) -> None:
        if not items:
            return
        if self.chat:
            ids = [int(it.get("id") or 0) for it in items if isinstance(it, dict)]
            latest = max(ids) if ids else 0
            if latest:
                self.page.run(lambda: self.page.api.mark_chat_read(latest),
                              lambda _r: self.page.refresh_badge(), label="聊天已读")
            return
        # 首屏已是最新一页：交给服务端以「当前时间」为界标记，
        # 避免客户端时钟偏差或时间串比较误差导致漏标最新一条。
        self.page.run(lambda: self.page.api.mark_messages_read(self.category),
                      lambda _r: self.page.refresh_badge(), label="消息已读")

    # ── 行渲染 ──
    def _row(self, item: dict) -> QWidget:
        if self.chat:
            return self._chat_row(item)
        if self.category == "system":
            return self._system_row(item)
        if self.category == "likes":
            return self._like_row(item)
        return self._comment_row(item)

    def _open_post(self, post_id: str) -> None:
        if post_id:
            self.page.open_post(str(post_id))

    def _like_row(self, item: dict) -> QWidget:
        actor = item.get("actor") or {}
        is_comment = str(item.get("target_type") or "") == "comment"
        title = "%s 赞了你的%s" % (
            str(actor.get("name") or "有人"), "评论" if is_comment else "帖子")
        body = str(item.get("comment_excerpt") or "") if is_comment \
            else str(item.get("post_title") or "")
        post_id = str(item.get("post_id") or "")
        return _MessageRow(
            title, _strip(body), item.get("created_at"),
            avatar=str(actor.get("avatar") or ""), on_open=lambda: self._open_post(post_id),
        )

    def _comment_row(self, item: dict) -> QWidget:
        actor = item.get("actor") or {}
        title = "%s %s" % (str(actor.get("name") or "有人"),
                           "回复了你" if item.get("parent_id") else "评论了你的帖子")
        post_id = str(item.get("post_id") or "")
        body = str(item.get("excerpt") or "")
        if not body:
            body = str(item.get("post_title") or "")
        return _MessageRow(
            title, _strip(body), item.get("created_at"),
            avatar=str(actor.get("avatar") or ""), on_open=lambda: self._open_post(post_id),
        )

    def _system_row(self, item: dict) -> QWidget:
        link = str(item.get("link") or "")
        return _MessageRow(
            str(item.get("title") or "系统通知"), _strip(item.get("body_html")),
            item.get("created_at"),
            on_open=(lambda: self.page.open_link(link)) if link else None,
        )

    def _chat_row(self, item: dict) -> QWidget:
        mine = str(item.get("sender_type") or "") == "user"
        title = "我" if mine else str(item.get("sender_name") or "管理团队")
        return _MessageRow(
            title, _strip(item.get("content_html")), item.get("created_at"),
            avatar="" if mine else str(item.get("sender_avatar") or ""),
            mine=mine,
        )

    # ── 发送 ──
    def send(self) -> None:
        if not self.chat:
            return
        if not self.page.need_login("请先登录后再留言"):
            return
        content = self.input.text().strip()
        if not content:
            return
        self.send_btn.setEnabled(False)

        def _done(result) -> None:
            self.send_btn.setEnabled(True)
            if result.ok:
                self.input.clear()
                self.reload()
            else:
                self.page.toast(result.message or "发送失败")

        def _fail(message: str) -> None:
            self.send_btn.setEnabled(True)
            self.page.toast(message)

        self.page.run(lambda: self.page.api.send_chat(_to_html(content)), _done, _fail,
                      label="发送团队消息")


class MessagesPage(Page):
    """消息中心。"""

    ROUTE = "messages"
    TITLE = "消息中心"
    SHOW_WORLD_PANEL = False

    def __init__(self, shell=None, parent: QWidget | None = None) -> None:
        super().__init__(shell, parent)
        root = vbox(self, margins=(12, 12, 12, 12), spacing=10)
        header, _ = self.make_header("消息中心", "点赞 / 评论 / 系统 / 团队消息")
        header.actions.addWidget(button("刷新", "ghost", self.refresh))
        root.addWidget(header)

        self.tabs = QTabWidget(self)
        self.lists: dict[str, _MessageList] = {}
        color = theme.palette()["text_secondary"]
        for key, label, icon_name in TABS:
            widget = _MessageList(self, key, chat=(key == "chat"))
            self.lists[key] = widget
            index = self.tabs.addTab(widget, label)
            self.tabs.setTabIcon(index, icons.icon(icon_name, 14, color))
        root.addWidget(self.tabs, 1)
        self.tabs.currentChanged.connect(self._on_tab_changed)

    def on_show(self, **kwargs) -> None:
        super().on_show(**kwargs)
        if not self.need_login("请先登录后查看消息"):
            return
        self.refresh()

    def _on_tab_changed(self, index: int) -> None:
        key = TABS[max(0, min(index, len(TABS) - 1))][0]
        widget = self.lists[key]
        if not widget.loaded and not widget.busy:
            widget.reload()

    def refresh(self) -> None:
        if not self.api.user:
            return
        self.refresh_badge()
        current = self.tabs.currentIndex()
        key = TABS[max(0, min(current, len(TABS) - 1))][0]
        self.lists[key].reload()

    def refresh_badge(self) -> None:
        self.run(self.api.message_summary, self._apply_summary, label="消息未读")

    def _apply_summary(self, result) -> None:
        if not result.ok:
            return
        for index, (key, label, _icon) in enumerate(TABS):
            unread = int(result.get(key) or 0)
            self.tabs.setTabText(index, label + (" (%d)" % unread if unread else ""))
        total = sum(int(result.get(key) or 0) for key, _label, _icon in TABS)
        badge = getattr(self.shell, "set_message_badge", None)
        if callable(badge):
            badge(total)
