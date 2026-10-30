import Cocoa
import SQLite3
import Carbon.HIToolbox
import Darwin

// MARK: - Constants

private let dbPath       = NSHomeDirectory() + "/.config/clipd/history.db"
private let suppressPath = NSHomeDirectory() + "/.config/clipd/.suppress"
private let silencePath  = NSHomeDirectory() + "/.config/clipd/.silent"
private let tailscaleOffPath = NSHomeDirectory() + "/.config/clipd/.tailscale_off"
private let axPromptedKey = "ClipBar.didPromptAccessibility"
private let interceptCmdVKey = "ClipBar.interceptCmdV"

private let localHostName: String = {
    var buf = [CChar](repeating: 0, count: 256)
    guard gethostname(&buf, buf.count) == 0 else { return "" }
    return String(cString: buf)
}()

private func sameHost(_ a: String, _ b: String) -> Bool {
    if a.isEmpty || b.isEmpty { return false }
    let aa = a.lowercased()
    let bb = b.lowercased()
    return aa == bb || aa.split(separator: ".").first == bb.split(separator: ".").first
}

private func timeAgo(_ ts: Double) -> String {
    let d = Date().timeIntervalSince1970 - ts
    if d < 60    { return "\(Int(d))s" }
    if d < 3600  { return "\(Int(d / 60))m" }
    if d < 86400 { return "\(Int(d / 3600))h" }
    return "\(Int(d / 86400))d"
}

// MARK: - Model

struct ClipItem {
    let id: Int64
    let type: String
    let content: String?
    let imagePath: String?
    let contentHash: String
    let preview: String
    let createdAt: Double
    let sourceMac: String
}

// MARK: - Database

final class ClipDatabase {
    private var db: OpaquePointer?

    init() {
        sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READWRITE | SQLITE_OPEN_FULLMUTEX, nil)
        sqlite3_exec(db, "PRAGMA journal_mode=WAL;", nil, nil, nil)
    }
    deinit { sqlite3_close(db) }

    func items(query: String = "") -> [ClipItem] {
        guard db != nil else { return [] }
        let sql = query.isEmpty
            ? "SELECT id,type,content,image_path,preview,created_at,source_mac,content_hash FROM items ORDER BY created_at DESC LIMIT 200"
            : "SELECT id,type,content,image_path,preview,created_at,source_mac,content_hash FROM items WHERE type='text' AND content LIKE ? ORDER BY created_at DESC LIMIT 200"
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return [] }
        defer { sqlite3_finalize(stmt) }
        if !query.isEmpty {
            let pattern = "%\(query)%"
            _ = pattern.withCString { ptr in
                sqlite3_bind_text(stmt, 1, ptr, -1, unsafeBitCast(-1, to: sqlite3_destructor_type.self))
            }
        }
        var out: [ClipItem] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            out.append(ClipItem(
                id:        sqlite3_column_int64(stmt, 0),
                type:      sqlite3_column_text(stmt, 1).map { String(cString: $0) } ?? "",
                content:   sqlite3_column_text(stmt, 2).map { String(cString: $0) },
                imagePath: sqlite3_column_text(stmt, 3).map { String(cString: $0) },
                contentHash: sqlite3_column_text(stmt, 7).map { String(cString: $0) } ?? "",
                preview:   sqlite3_column_text(stmt, 4).map { String(cString: $0) } ?? "",
                createdAt: sqlite3_column_double(stmt, 5),
                sourceMac: sqlite3_column_text(stmt, 6).map { String(cString: $0) } ?? ""
            ))
        }
        return out
    }

    func promote(_ id: Int64) {
        guard db != nil else { return }
        var stmt: OpaquePointer?
        sqlite3_prepare_v2(db, "UPDATE items SET created_at=? WHERE id=?", -1, &stmt, nil)
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_double(stmt, 1, Date().timeIntervalSince1970)
        sqlite3_bind_int64(stmt, 2, id)
        sqlite3_step(stmt)
    }
}

// MARK: - Table cell

private let cellID = NSUserInterfaceItemIdentifier("ClipCell")

final class ClipCell: NSTableCellView {
    private let thumb   = NSImageView()
    private let preview = NSTextField(labelWithString: "")
    private let meta    = NSTextField(labelWithString: "")
    private var thumbW: NSLayoutConstraint!
    private var thumbH: NSLayoutConstraint!

    override init(frame: NSRect) {
        super.init(frame: frame)

        thumb.translatesAutoresizingMaskIntoConstraints = false
        thumb.imageScaling  = .scaleProportionallyUpOrDown
        thumb.wantsLayer    = true
        thumb.layer?.cornerRadius   = 6
        thumb.layer?.masksToBounds  = true
        thumb.layer?.borderWidth    = 1
        thumb.layer?.borderColor    = NSColor.separatorColor.cgColor
        thumb.contentTintColor = .secondaryLabelColor

        preview.translatesAutoresizingMaskIntoConstraints = false
        preview.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        preview.lineBreakMode = .byTruncatingTail
        preview.maximumNumberOfLines = 2
        preview.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        meta.translatesAutoresizingMaskIntoConstraints = false
        meta.font      = NSFont.systemFont(ofSize: 10)
        meta.textColor = .secondaryLabelColor

        [thumb, preview, meta].forEach { addSubview($0) }
        thumbW = thumb.widthAnchor.constraint(equalToConstant: 36)
        thumbH = thumb.heightAnchor.constraint(equalToConstant: 36)
        NSLayoutConstraint.activate([
            thumb.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 10),
            thumb.centerYAnchor.constraint(equalTo: centerYAnchor),
            thumbW, thumbH,

            preview.leadingAnchor.constraint(equalTo: thumb.trailingAnchor, constant: 10),
            preview.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -10),
            preview.topAnchor.constraint(equalTo: topAnchor, constant: 10),

            meta.leadingAnchor.constraint(equalTo: preview.leadingAnchor),
            meta.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -8),
            meta.trailingAnchor.constraint(equalTo: preview.trailingAnchor),
        ])
    }
    required init?(coder: NSCoder) { fatalError() }

    func configure(_ item: ClipItem) {
        if item.type == "image", let data = ImageStore.read(item.contentHash, path: item.imagePath),
           let img = NSImage(data: data) {
            thumb.image = img
            thumb.contentTintColor = nil
            thumb.layer?.borderWidth = 1
        } else {
            thumb.image = NSImage(systemSymbolName: "doc.on.clipboard", accessibilityDescription: nil)
            thumb.contentTintColor = .secondaryLabelColor
            thumb.layer?.borderWidth = 0
        }
        thumbW.constant = 36
        thumbH.constant = 36
        preview.stringValue = item.preview
        let src  = sameHost(item.sourceMac, localHostName) ? "" : " · \(item.sourceMac)"
        meta.stringValue = timeAgo(item.createdAt) + src
    }
}

// MARK: - Table view (intercepts Enter/Escape)

final class ClipTableView: NSTableView {
    var onEnter: (() -> Void)?
    var onEscape: (() -> Void)?
    override func keyDown(with event: NSEvent) {
        switch event.keyCode {
        case 36, 76: onEnter?()
        case 53:     onEscape?()
        default:     super.keyDown(with: event)
        }
    }
}

// MARK: - View controller

// Layout constants — kept together so the panel + view stay in sync.
let kListWidth:    CGFloat = 400
let kPreviewWidth: CGFloat = 320
let kPanelHeight:  CGFloat = 520
/// Fraction of the screen's visible height the panel should occupy.
let kPanelHeightFraction: CGFloat = 0.4
let kPanelHeightMin: CGFloat = 240
let kPanelHeightMax: CGFloat = 600

final class ClipViewController: NSViewController,
                                NSTableViewDataSource, NSTableViewDelegate,
                                NSSearchFieldDelegate {

    private let search       = NSSearchField()
    private let scroll       = NSScrollView()
    private let table        = ClipTableView()
    private let empty        = NSTextField(labelWithString: "Nothing copied yet")
    private let previewBox   = NSView()
    private let previewImage = NSImageView()
    private let previewMeta  = NSTextField(labelWithString: "")
    private var scrollTopHidden: NSLayoutConstraint!
    private var scrollTopShown:  NSLayoutConstraint!
    private(set) var isSearchVisible = false

    private let db    = ClipDatabase()
    var items: [ClipItem] = []
    var onPick: ((ClipItem) -> Void)?
    /// Called when the user dismisses the panel without picking anything
    /// (Esc, click outside, ⌘V toggle while open). Wired to AppDelegate's
    /// closePanel so cleanup + focus restoration runs on every dismissal,
    /// not just the mouse-monitor path.
    var onDismiss: (() -> Void)?
    /// Called when the selection changes between an image item and a non-image
    /// item, so the panel can grow/shrink to reveal/hide the preview pane.
    var onPreviewVisibilityChange: ((Bool) -> Void)?
    private(set) var isPreviewVisible = false
    private var ticker: Timer?

    override func loadView() {
        view = NSView(frame: NSRect(x: 0, y: 0, width: kListWidth, height: kPanelHeight))
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        buildSearch()
        buildTable()
        buildEmpty()
        buildPreview()
    }

    override func viewWillAppear() {
        super.viewWillAppear()
        // Always reopen with search hidden and selection reset.
        search.stringValue   = ""
        search.isHidden      = true
        scrollTopShown.isActive  = false
        scrollTopHidden.isActive = true
        isSearchVisible = false
        reload(resetSelection: true)
        ticker = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: true) { [weak self] _ in
            self?.reload()
        }
        focusTable()
    }

    func focusTable() {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.view.window?.makeFirstResponder(self.table)
        }
    }

    private func buildSearch() {
        search.translatesAutoresizingMaskIntoConstraints = false
        search.placeholderString = "Search…"
        search.target   = self
        search.action   = #selector(searchChanged)
        search.delegate = self
        search.isHidden = true
        view.addSubview(search)
        NSLayoutConstraint.activate([
            search.topAnchor.constraint(equalTo: view.topAnchor, constant: 10),
            search.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 10),
            search.widthAnchor.constraint(equalToConstant: kListWidth - 20),
        ])
    }

    func showSearch() {
        search.stringValue   = ""
        search.isHidden      = false
        scrollTopHidden.isActive = false
        scrollTopShown.isActive  = true
        isSearchVisible = true
        reload()
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.view.window?.makeFirstResponder(self.search)
        }
    }

    func hideSearch() {
        search.stringValue   = ""
        search.isHidden      = true
        scrollTopShown.isActive  = false
        scrollTopHidden.isActive = true
        isSearchVisible = false
        reload()
        focusTable()
    }

    @objc private func searchChanged() { reload() }

    private func moveSelection(by delta: Int) {
        guard !items.isEmpty else { return }
        let cur  = table.selectedRow
        var next = cur < 0 ? (delta > 0 ? 0 : items.count - 1) : cur + delta
        next = max(0, min(items.count - 1, next))
        table.selectRowIndexes([next], byExtendingSelection: false)
        table.scrollRowToVisible(next)
    }

    private func jumpSelection(toEnd: Bool) {
        guard !items.isEmpty else { return }
        let i = toEnd ? items.count - 1 : 0
        table.selectRowIndexes([i], byExtendingSelection: false)
        table.scrollRowToVisible(i)
    }

    // MARK: NSSearchFieldDelegate — forward navigation keys to the table.

    func control(_ control: NSControl, textView: NSTextView,
                 doCommandBy sel: Selector) -> Bool {
        switch sel {
        case #selector(NSResponder.moveDown(_:)):
            moveSelection(by: +1); return true
        case #selector(NSResponder.moveUp(_:)):
            moveSelection(by: -1); return true
        case #selector(NSResponder.moveToBeginningOfDocument(_:)),
             #selector(NSResponder.scrollToBeginningOfDocument(_:)):
            jumpSelection(toEnd: false); return true
        case #selector(NSResponder.moveToEndOfDocument(_:)),
             #selector(NSResponder.scrollToEndOfDocument(_:)):
            jumpSelection(toEnd: true); return true
        case #selector(NSResponder.scrollPageDown(_:)),
             #selector(NSResponder.pageDown(_:)):
            moveSelection(by: +6); return true
        case #selector(NSResponder.scrollPageUp(_:)),
             #selector(NSResponder.pageUp(_:)):
            moveSelection(by: -6); return true
        case #selector(NSResponder.insertNewline(_:)):
            activateRow(table.selectedRow); return true
        case #selector(NSResponder.cancelOperation(_:)):
            hideSearch(); return true
        default:
            return false
        }
    }

    override func viewDidDisappear() {
        super.viewDidDisappear()
        ticker?.invalidate()
        ticker = nil
    }

    private func buildTable() {
        let col = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("main"))
        col.isEditable = false
        table.addTableColumn(col)
        table.headerView = nil
        table.rowHeight  = 54
        table.intercellSpacing = NSSize(width: 0, height: 2)
        table.dataSource  = self
        table.delegate    = self
        table.onEnter  = { [weak self] in self?.activateRow(self?.table.selectedRow ?? -1) }
        table.onEscape = { [weak self] in self?.onDismiss?() }
        table.doubleAction = #selector(rowDoubleClicked)
        table.target = self

        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.borderType = .noBorder
        view.addSubview(scroll)
        scrollTopHidden = scroll.topAnchor.constraint(equalTo: view.topAnchor, constant: 8)
        scrollTopShown  = scroll.topAnchor.constraint(equalTo: search.bottomAnchor, constant: 8)
        NSLayoutConstraint.activate([
            scrollTopHidden,
            scroll.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scroll.widthAnchor.constraint(equalToConstant: kListWidth),
            scroll.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
    }

    private func buildEmpty() {
        empty.translatesAutoresizingMaskIntoConstraints = false
        empty.textColor = .secondaryLabelColor
        empty.font      = NSFont.systemFont(ofSize: 13)
        empty.alignment = .center
        view.addSubview(empty)
        NSLayoutConstraint.activate([
            empty.centerXAnchor.constraint(equalTo: scroll.centerXAnchor),
            empty.centerYAnchor.constraint(equalTo: scroll.centerYAnchor),
        ])
    }

    private func buildPreview() {
        previewBox.translatesAutoresizingMaskIntoConstraints = false
        previewBox.wantsLayer = true
        previewBox.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        previewBox.isHidden = true

        previewImage.translatesAutoresizingMaskIntoConstraints = false
        previewImage.imageScaling   = .scaleProportionallyUpOrDown
        previewImage.imageAlignment = .alignCenter
        previewImage.wantsLayer     = true
        previewImage.layer?.cornerRadius  = 8
        previewImage.layer?.masksToBounds = true
        previewImage.layer?.borderWidth   = 1
        previewImage.layer?.borderColor   = NSColor.separatorColor.cgColor

        previewMeta.translatesAutoresizingMaskIntoConstraints = false
        previewMeta.font      = NSFont.systemFont(ofSize: 11)
        previewMeta.textColor = .secondaryLabelColor
        previewMeta.alignment = .center

        previewBox.addSubview(previewImage)
        previewBox.addSubview(previewMeta)
        view.addSubview(previewBox)

        NSLayoutConstraint.activate([
            previewBox.topAnchor.constraint(equalTo: view.topAnchor, constant: 10),
            previewBox.leadingAnchor.constraint(equalTo: scroll.trailingAnchor, constant: 0),
            previewBox.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -10),
            previewBox.widthAnchor.constraint(equalToConstant: kPreviewWidth),

            previewImage.topAnchor.constraint(equalTo: previewBox.topAnchor, constant: 12),
            previewImage.leadingAnchor.constraint(equalTo: previewBox.leadingAnchor, constant: 12),
            previewImage.trailingAnchor.constraint(equalTo: previewBox.trailingAnchor, constant: -12),
            previewImage.bottomAnchor.constraint(equalTo: previewMeta.topAnchor, constant: -8),

            previewMeta.leadingAnchor.constraint(equalTo: previewBox.leadingAnchor, constant: 12),
            previewMeta.trailingAnchor.constraint(equalTo: previewBox.trailingAnchor, constant: -12),
            previewMeta.bottomAnchor.constraint(equalTo: previewBox.bottomAnchor, constant: -12),
        ])
    }

    private func updatePreview(for row: Int) {
        var shouldShow = false
        if row >= 0, row < items.count {
            let item = items[row]
            if item.type == "image",
               let data = ImageStore.read(item.contentHash, path: item.imagePath),
               let img = NSImage(data: data) {
                previewImage.image = img
                let dims = "\(Int(img.size.width)) × \(Int(img.size.height))"
                previewMeta.stringValue = dims
                shouldShow = true
            }
        }
        if shouldShow != isPreviewVisible {
            isPreviewVisible = shouldShow
            previewBox.isHidden = !shouldShow
            onPreviewVisibilityChange?(shouldShow)
        }
    }

    func reload(resetSelection: Bool = false) {
        let prev = resetSelection ? -1 : table.selectedRow
        items = db.items(query: isSearchVisible ? search.stringValue : "")
        table.reloadData()
        empty.isHidden = !items.isEmpty
        if items.isEmpty {
            updatePreview(for: -1)
            return
        }
        let sel = (prev >= 0 && prev < items.count) ? prev : 0
        table.selectRowIndexes([sel], byExtendingSelection: false)
        table.scrollRowToVisible(sel)
        updatePreview(for: sel)
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        updatePreview(for: table.selectedRow)
    }

    @objc private func rowDoubleClicked() { activateRow(table.clickedRow) }

    private func activateRow(_ row: Int) {
        guard row >= 0, row < items.count else { return }
        let item = items[row]
        db.promote(item.id)
        onPick?(item)
    }

    // MARK: NSTableView

    func numberOfRows(in tableView: NSTableView) -> Int { items.count }

    func tableView(_ tableView: NSTableView,
                   viewFor tableColumn: NSTableColumn?,
                   row: Int) -> NSView? {
        let cell = tableView.makeView(withIdentifier: cellID, owner: nil) as? ClipCell
                   ?? ClipCell(frame: .zero)
        cell.identifier = cellID
        cell.configure(items[row])
        return cell
    }
}

// MARK: - Panel that can take key focus while .nonactivatingPanel
//        keeps the parent app from stealing activation from the previous app.

final class FocusablePanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

// MARK: - Carbon hotkey trampoline (C handler -> AppDelegate.handleHotkey)

private func hotKeyHandler(
    _: EventHandlerCallRef?, _ event: EventRef?, _ userData: UnsafeMutableRawPointer?
) -> OSStatus {
    guard let userData else { return noErr }
    var hkid = EventHotKeyID()
    if let event = event {
        GetEventParameter(event,
                          EventParamName(kEventParamDirectObject),
                          EventParamType(typeEventHotKeyID),
                          nil, MemoryLayout<EventHotKeyID>.size, nil, &hkid)
    }
    let app = Unmanaged<AppDelegate>.fromOpaque(userData).takeUnretainedValue()
    let id = hkid.id
    DispatchQueue.main.async { app.handleHotkey(id: id) }
    return noErr
}

// MARK: - App delegate

class AppDelegate: NSObject, NSApplicationDelegate {

    private var statusItem: NSStatusItem!
    private var panel: FocusablePanel!
    private var clipVC: ClipViewController!
    private var hotKeyRef: EventHotKeyRef?
    private var cmdVHotKeyRef: EventHotKeyRef?
    private var mouseMonitor: Any?
    private var keyMonitor: Any?
    private var previousApp: NSRunningApplication?
    private var axTrusted = false
    private var axWatcher: Timer?
    private let db = ClipDatabase()
    /// Snapshot of what ⌘V auto-pasted, so we can reverse it if the user
    /// picks a different item from the panel. `item` is the matching DB row
    /// when we found one (ID-based same-item detection); `pastedText` is the
    /// actual text put into the target so backspace-undo can count chars.
    private struct AutoPasteSnapshot {
        let item: ClipItem?
        let pastedText: String?
    }
    private var lastAutoPaste: AutoPasteSnapshot?

    private var autoPasteOnCmdV: Bool {
        let d = UserDefaults.standard
        if d.object(forKey: interceptCmdVKey) == nil { return true }
        return d.bool(forKey: interceptCmdVKey)
    }

    func applicationDidFinishLaunching(_ n: Notification) {
        setupStatusItem()
        setupPanel()
        registerHotkey()
        axTrusted = AXIsProcessTrusted()
        NSLog("ClipBar: ready, ⌘⇧V registered. AXIsProcessTrusted=\(axTrusted)")
        startAXWatcher()
    }

    /// macOS doesn't notify a running process when its TCC trust flips on;
    /// AXIsProcessTrusted() in this process keeps returning false until we
    /// relaunch. Poll once a second; when trust flips false→true and the
    /// panel is closed, exit so launchd KeepAlive relaunches us cleanly.
    private func startAXWatcher() {
        axWatcher = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            guard let self else { return }
            let now = AXIsProcessTrusted()
            if now != self.axTrusted {
                NSLog("ClipBar: AXIsProcessTrusted \(self.axTrusted) → \(now)")
                self.axTrusted = now
                if now, self.panel?.isVisible == false {
                    NSLog("ClipBar: AX granted — relaunching to pick up permission")
                    exit(0)
                }
            }
        }
    }

    // MARK: Status bar

    private func setupStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "doc.on.clipboard",
                                           accessibilityDescription: "ClipBar")
        statusItem.button?.target = self
        statusItem.button?.action = #selector(statusClicked(_:))
        statusItem.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
    }

    @objc private func statusClicked(_ sender: NSStatusBarButton) {
        let evt = NSApp.currentEvent
        if evt?.type == .rightMouseUp || (evt?.modifierFlags.contains(.control) ?? false) {
            showMenu()
        } else {
            togglePanel()
        }
    }

    private func showMenu() {
        let menu = NSMenu()
        let axOK = AXIsProcessTrusted()
        let axTitle = axOK ? "Auto-paste: enabled ✓" : "Enable Auto-paste…"
        // Always clickable. When granted, take the user to the Accessibility
        // pane so they can revoke or inspect the toggle (otherwise the row
        // looks broken because AppKit grays out items with a nil action).
        let axItem = NSMenuItem(title: axTitle,
                                action: axOK ? #selector(openAccessibilitySettings)
                                             : #selector(requestAccessibility),
                                keyEquivalent: "")
        axItem.target = self
        menu.addItem(axItem)

        let cmdVItem = NSMenuItem(title: "Open on ⌘V",
                                  action: #selector(toggleCmdVIntercept),
                                  keyEquivalent: "")
        cmdVItem.state = autoPasteOnCmdV ? .on : .off
        cmdVItem.target = self
        menu.addItem(cmdVItem)

        let soundItem = NSMenuItem(title: "Capture sound",
                                   action: #selector(toggleCaptureSound),
                                   keyEquivalent: "")
        soundItem.state = FileManager.default.fileExists(atPath: silencePath) ? .off : .on
        soundItem.target = self
        menu.addItem(soundItem)

        addSyncSection(to: menu)

        let restartItem = NSMenuItem(title: "Restart ClipBar",
                                     action: #selector(restart),
                                     keyEquivalent: "r")
        restartItem.target = self
        menu.addItem(restartItem)

        menu.addItem(.separator())

        let quitItem = NSMenuItem(title: "Quit ClipBar",
                                  action: #selector(quit),
                                  keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)
        statusItem.menu = menu
        statusItem.button?.performClick(nil)
        statusItem.menu = nil  // restore left-click toggle behavior
    }

    @objc private func quit() { NSApp.terminate(nil) }
    @objc private func restart() { exit(0) }

    @objc private func requestAccessibility() {
        let opts = ["AXTrustedCheckOptionPrompt": true] as CFDictionary
        AXIsProcessTrustedWithOptions(opts)
        UserDefaults.standard.set(true, forKey: axPromptedKey)
    }

    @objc private func openAccessibilitySettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func toggleCmdVIntercept() {
        let next = !autoPasteOnCmdV
        UserDefaults.standard.set(next, forKey: interceptCmdVKey)
        NSLog("ClipBar: ⌘V intercept → \(next), restarting…")
        exit(0)
    }

    /// Toggle the capture-sound flag clipd reads. Touch creates the silence
    /// flag; removing it re-enables sound. No restart needed — clipd checks
    /// the flag each time it would play.
    @objc private func toggleCaptureSound() {
        let fm = FileManager.default
        if fm.fileExists(atPath: silencePath) {
            try? fm.removeItem(atPath: silencePath)
            NSLog("ClipBar: capture sound → on")
        } else {
            fm.createFile(atPath: silencePath, contents: nil)
            NSLog("ClipBar: capture sound → off")
        }
    }

    // MARK: Sync menu section

    /// Build the Sync sub-section: a "Sync over Tailscale" toggle plus a
    /// disabled informational row per known peer. The peer status is
    /// fetched synchronously from clipd's /peers (localhost — fast).
    private func addSyncSection(to menu: NSMenu) {
        let snap = fetchPeerSnapshot()

        menu.addItem(.separator())

        let header = NSMenuItem(title: "Sync", action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)

        let tsItem = NSMenuItem(title: "Sync over Tailscale",
                                action: #selector(toggleTailscale),
                                keyEquivalent: "")
        let tsAvailable = snap?.tailscaleAvailable ?? true
        let tsEnabled   = snap?.tailscaleEnabled ?? !FileManager.default.fileExists(atPath: tailscaleOffPath)
        tsItem.state = (tsAvailable && tsEnabled) ? .on : .off
        tsItem.target = self
        if !tsAvailable {
            tsItem.toolTip = "Tailscale binary not found. Install Tailscale to enable cross-internet sync."
        } else {
            tsItem.toolTip = "Sync clipboard with other ClipBar devices on your tailnet."
        }
        menu.addItem(tsItem)

        // Peer list. Combine LAN + Tailscale rows under a single header,
        // tagged with the transport. If a hostname appears on both sides
        // (LAN-discovered AND tailnet-discovered), show only the LAN row —
        // that's the one in active use given the dedup rule.
        let rows = peerRows(from: snap)
        if rows.isEmpty {
            let none = NSMenuItem(title: "  No peers found",
                                  action: nil, keyEquivalent: "")
            none.isEnabled = false
            menu.addItem(none)
        } else {
            for r in rows {
                let item = NSMenuItem(title: "  " + r,
                                      action: nil, keyEquivalent: "")
                item.isEnabled = false
                menu.addItem(item)
            }
        }
    }

    @objc private func toggleTailscale() {
        let fm = FileManager.default
        if fm.fileExists(atPath: tailscaleOffPath) {
            try? fm.removeItem(atPath: tailscaleOffPath)
            NSLog("ClipBar: tailscale sync → on")
        } else {
            fm.createFile(atPath: tailscaleOffPath, contents: nil)
            NSLog("ClipBar: tailscale sync → off")
        }
    }

    // MARK: Peer status fetch

    private struct PeerSnapshot {
        let lan: [(host: String, transport: String)]
        let tailscale: [(host: String, direction: String)]
        let tailscaleAvailable: Bool
        let tailscaleEnabled: Bool
    }

    private func fetchPeerSnapshot() -> PeerSnapshot? {
        guard let d = PeerStatus.read(),
              let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else {
            return nil
        }
        let lanRaw = (obj["lan"] as? [[String: Any]]) ?? []
        let tsRaw  = (obj["tailscale"] as? [[String: Any]]) ?? []
        let lan = lanRaw.compactMap { d -> (host: String, transport: String)? in
            guard let h = d["hostname"] as? String else { return nil }
            return (h, "LAN")
        }
        let ts = tsRaw.compactMap { d -> (host: String, direction: String)? in
            guard let h = d["hostname"] as? String else { return nil }
            let dir = (d["direction"] as? String) ?? ""
            return (h, dir)
        }
        return PeerSnapshot(
            lan: lan,
            tailscale: ts,
            tailscaleAvailable: (obj["tailscale_available"] as? Bool) ?? false,
            tailscaleEnabled: (obj["tailscale_enabled"] as? Bool) ?? false
        )
    }

    private func peerRows(from snap: PeerSnapshot?) -> [String] {
        guard let snap else { return [] }
        // Strip any trailing tailnet/.local suffix so the leading label
        // is the dedup key. (e.g. "host.tailnet.ts.net" → "host".)
        func base(_ s: String) -> String {
            s.split(separator: ".").first.map(String.init)?.lowercased() ?? s.lowercased()
        }
        var seen = Set<String>()
        var out: [String] = []
        for p in snap.lan {
            let key = base(p.host)
            if seen.insert(key).inserted {
                out.append("\(p.host) — LAN")
            }
        }
        for p in snap.tailscale {
            let key = base(p.host)
            if seen.insert(key).inserted {
                let arrow = p.direction == "out" ? "→" : (p.direction == "in" ? "←" : "↔")
                out.append("\(p.host) — Tailscale \(arrow)")
            }
        }
        return out
    }

    // MARK: Panel

    private func setupPanel() {
        clipVC = ClipViewController()
        clipVC.onPick = { [weak self] item in self?.paste(item) }
        clipVC.onDismiss = { [weak self] in self?.closePanel() }
        clipVC.onPreviewVisibilityChange = { [weak self] showing in
            self?.resizePanel(forPreview: showing)
        }

        panel = FocusablePanel(
            contentRect: NSRect(x: 0, y: 0, width: kListWidth, height: kPanelHeight),
            styleMask: [.fullSizeContentView, .borderless, .resizable],
            backing: .buffered, defer: false
        )
        panel.isFloatingPanel = true
        panel.becomesKeyOnlyIfNeeded = false
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.isMovableByWindowBackground = false
        panel.hidesOnDeactivate = false
        panel.hasShadow = true
        panel.backgroundColor = NSColor.windowBackgroundColor
        panel.contentView = clipVC.view
        clipVC.view.layoutSubtreeIfNeeded()
    }

    @objc func togglePanel() { togglePanel(autoPaste: false) }

    func handleHotkey(id: UInt32) {
        if id == 2 {
            togglePanel(autoPaste: true)
        } else {
            togglePanel(autoPaste: false)
        }
    }

    func togglePanel(autoPaste: Bool) {
        if panel.isVisible {
            closePanel()
        } else {
            openPanel(autoPaste: autoPaste)
        }
    }

    private func openPanel(autoPaste: Bool = false) {
        // Remember who was frontmost so we can paste back into them.
        if let front = NSWorkspace.shared.frontmostApplication,
           front.bundleIdentifier != Bundle.main.bundleIdentifier {
            previousApp = front
        }

        if autoPaste, AXIsProcessTrusted(), let prev = previousApp {
            let pb = NSPasteboard.general
            let pid = prev.processIdentifier

            // A Carbon hotkey receives every ⌘V before the focused app does.
            // File and folder copies are intentionally outside ClipBar's
            // scope, so leave their pasteboard data untouched and immediately
            // deliver the original shortcut to its destination. Finder puts
            // file copies on the pasteboard as either modern file URLs or
            // the older NSFilenamesPboardType; some apps use file promises.
            if containsFiles(clipboard: pb) {
                lastAutoPaste = nil
                simulateKey(virtualKey: 0x09, flags: .maskCommand, toPid: pid)
                NSLog("ClipBar: ⌘V passthrough — file or folder clipboard")
                return
            }
            let match = findItemMatching(clipboard: pb)

            if let match = match, writeClipboard(for: match) {
                // Clipboard contents are tracked in our history — own the
                // paste so images get the path-string fallback for text-only
                // targets, and remember the item so the panel can offer a
                // clean swap-with-undo.
                lastAutoPaste = AutoPasteSnapshot(item: match, pastedText: match.content)
                simulateKey(virtualKey: 0x09, flags: .maskCommand, toPid: pid)
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.10) { [weak self] in
                    self?.presentPanel()
                }
            } else {
                // Clipboard has content we don't recognize — usually because
                // another tool (voice dictation, AppleScript) set it
                // microseconds before our hotkey fired and clipd hasn't
                // logged it yet. Don't clobber their write, don't open the
                // panel (it would interrupt the dictation flow); just pass
                // ⌘V through. If the user wants the picker they can ⌘⇧V.
                lastAutoPaste = nil
                simulateKey(virtualKey: 0x09, flags: .maskCommand, toPid: pid)
                NSLog("ClipBar: ⌘V passthrough — clipboard contents not in history")
            }
        } else {
            lastAutoPaste = nil
            presentPanel()
        }
    }

    /// Whether a pasteboard contains a file/folder transfer. This deliberately
    /// checks declared types rather than reading URL data: accessing a file
    /// promise can cause the provider to materialize it, which would itself
    /// alter the native paste workflow we are trying to preserve.
    private func containsFiles(clipboard pb: NSPasteboard) -> Bool {
        let fileTypes: Set<NSPasteboard.PasteboardType> = [
            .fileURL,
            NSPasteboard.PasteboardType("NSFilenamesPboardType"),
            NSPasteboard.PasteboardType("com.apple.pasteboard.promised-file-url"),
            NSPasteboard.PasteboardType("com.apple.NSFilePromiseItemMetaData"),
        ]
        return pb.pasteboardItems?.contains { item in
            !fileTypes.isDisjoint(with: Set(item.types))
        } ?? false
    }

    /// Search the (recent) history for an item whose content matches what's
    /// currently on the pasteboard. Walks all rows — not just the top —
    /// because after we writeClipboard for a swap clipd is suppressed and the
    /// "top" row stays stale relative to the live clipboard.
    private func findItemMatching(clipboard pb: NSPasteboard) -> ClipItem? {
        let items = db.items()
        if let s = pb.string(forType: .string), !s.isEmpty {
            if let m = items.first(where: { $0.type == "text" && $0.content == s }) {
                return m
            }
        }
        if pb.data(forType: .png) != nil || pb.data(forType: .tiff) != nil {
            // Loose image match — comparing bytes is expensive and the
            // misidentification cost (showing the panel for a different
            // image) is just a stray ⌘Z attempt on swap.
            if let m = items.first(where: { $0.type == "image" }) {
                return m
            }
        }
        return nil
    }

    private func presentPanel() {
        positionPanel()
        // We must activate the app before makeKeyAndOrderFront, otherwise
        // LSUIElement apps don't get key window status and arrow keys go
        // nowhere. previousApp is already saved so paste can restore focus.
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        clipVC.focusTable()
        clipVC.reload()

        mouseMonitor = NSEvent.addGlobalMonitorForEvents(
            matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            self?.closePanel()
        }
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self else { return event }
            return self.handlePanelKey(event)
        }
    }

    /// Decide whether a key pressed while the panel is key should pass through
    /// to the table (navigation / activate / dismiss) or dismiss the panel and
    /// be forwarded to the originating app so the user keeps typing where they
    /// were before the panel opened.
    private func handlePanelKey(_ event: NSEvent) -> NSEvent? {
        // When search is open, pass everything through — the search field's
        // delegate handles navigation keys, Enter, and Escape.
        if clipVC.isSearchVisible {
            return event
        }
        // Virtual keycodes that drive picker navigation.
        // 36 return, 76 numpad enter, 53 escape,
        // 123 left, 124 right, 125 down, 126 up,
        // 115 home, 119 end, 116 page up, 121 page down.
        let navKeys: Set<UInt16> = [36, 76, 53, 123, 124, 125, 126, 115, 119, 116, 121]
        if navKeys.contains(event.keyCode) {
            return event
        }
        // / or ⌘F → open search instead of dismissing.
        let ch   = event.charactersIgnoringModifiers ?? ""
        let mods = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if ch == "/" && mods.isEmpty {
            clipVC.showSearch(); return nil
        }
        if ch == "f" && mods == .command {
            clipVC.showSearch(); return nil
        }
        dismissAndForward(event: event)
        return nil
    }

    /// Hide the panel, hand focus back to the originating app, and replay the
    /// keystroke there so the user's typing lands in their original text box
    /// instead of being swallowed by ClipBar.
    private func dismissAndForward(event: NSEvent) {
        let kc = event.keyCode
        let mods = event.modifierFlags
        closePanel(restoreFocus: false)
        guard let prev = previousApp, !prev.isTerminated else { return }
        if #available(macOS 14, *) {
            NSApp.yieldActivation(to: prev)
            prev.activate(options: [])
        } else {
            prev.activate(options: [.activateIgnoringOtherApps])
        }
        guard AXIsProcessTrusted() else { return }
        let pid = prev.processIdentifier
        var flags: CGEventFlags = []
        if mods.contains(.command) { flags.insert(.maskCommand) }
        if mods.contains(.shift)   { flags.insert(.maskShift) }
        if mods.contains(.option)  { flags.insert(.maskAlternate) }
        if mods.contains(.control) { flags.insert(.maskControl) }
        waitForActive(prev, deadline: 0.5) { [weak self] success in
            guard let self, success else { return }
            self.simulateKey(virtualKey: CGKeyCode(kc), flags: flags, toPid: pid)
        }
    }

    /// Place the panel under the menu-bar icon, accounting for current width.
    /// Also resizes height to a fraction of the active screen so the list
    /// scrolls inside a compact panel instead of stretching the full display.
    private func positionPanel() {
        guard let btn = statusItem.button, let win = btn.window else { return }
        let btnScreen = win.convertToScreen(btn.frame)
        let screen    = NSScreen.screens.first { $0.frame.contains(btnScreen) } ?? NSScreen.main!
        let vis = screen.visibleFrame

        let targetH = min(max(round(vis.height * kPanelHeightFraction), kPanelHeightMin), kPanelHeightMax)
        var frame = panel.frame
        if abs(frame.size.height - targetH) > 0.5 {
            frame.size.height = targetH
            panel.setFrame(frame, display: false)
        }

        let pw = panel.frame.width, ph = panel.frame.height
        var x = btnScreen.midX - pw / 2
        var y = btnScreen.minY - ph - 4
        x = max(vis.minX + 8, min(x, vis.maxX - pw - 8))
        y = max(vis.minY + 8, y)
        panel.setFrameOrigin(NSPoint(x: x, y: y))
    }

    /// Animate panel width to reveal/hide the side preview pane.
    private func resizePanel(forPreview showing: Bool) {
        let targetWidth: CGFloat = showing ? (kListWidth + kPreviewWidth) : kListWidth
        var frame = panel.frame
        if abs(frame.size.width - targetWidth) < 0.5 { return }
        let dw = targetWidth - frame.size.width
        frame.size.width = targetWidth
        // Keep horizontally centered relative to current position.
        frame.origin.x -= dw / 2
        // Re-clamp to the visible screen.
        if let screen = NSScreen.screens.first(where: { $0.frame.intersects(frame) }) ?? NSScreen.main {
            let vis = screen.visibleFrame
            frame.origin.x = max(vis.minX + 8, min(frame.origin.x, vis.maxX - frame.size.width - 8))
        }
        panel.setFrame(frame, display: true, animate: true)
    }

    func closePanel(restoreFocus: Bool = true) {
        panel.orderOut(nil)
        if let m = mouseMonitor { NSEvent.removeMonitor(m); mouseMonitor = nil }
        if let m = keyMonitor   { NSEvent.removeMonitor(m); keyMonitor   = nil }
        // Dismissed without a pick → original ⌘V paste stands.
        lastAutoPaste = nil
        // Hand activation back to the originating app so the caret stays in
        // the user's text field. Skipped on the paste path because paste()
        // already drives its own yield+activate cycle around the keystroke.
        if restoreFocus, let prev = previousApp, !prev.isTerminated {
            if #available(macOS 14, *) {
                NSApp.yieldActivation(to: prev)
                prev.activate(options: [])
            } else {
                prev.activate(options: [.activateIgnoringOtherApps])
            }
        }
    }

    // MARK: Paste

    private func paste(_ item: ClipItem) {
        // The auto-paste already put this exact item into the user's app —
        // picking it again would just paste it twice. Same-content match
        // (different DB id) is also covered, which happens when the same
        // string lives in history under multiple rows.
        if let snap = lastAutoPaste {
            if let snapItem = snap.item, snapItem.id == item.id {
                closePanel()
                return
            }
            if item.type == "text",
               let pasted = snap.pastedText,
               item.content == pasted {
                closePanel()
                return
            }
        }
        // Capture before closePanel clears it.
        let undoTarget = lastAutoPaste

        guard writeClipboard(for: item) else {
            NSSound.beep()
            return
        }
        closePanel(restoreFocus: false)

        guard let prev = previousApp else { return }

        // Yield activation explicitly on macOS 14+, then activate. Without
        // yieldActivation, prev.activate() is sometimes a no-op because we're
        // still the active app.
        if #available(macOS 14, *) {
            NSApp.yieldActivation(to: prev)
            prev.activate(options: [])
        } else {
            prev.activate(options: [.activateIgnoringOtherApps])
        }

        // Wait for the activation to actually take effect before sending ⌘V —
        // otherwise the keystroke lands in ClipBar instead of the target app.
        // If AX isn't granted we can't paste, but the clipboard is already set.
        let trusted = AXIsProcessTrusted()
        NSLog("ClipBar: paste — AX=\(trusted), undo=\(undoTarget != nil), target=\(prev.bundleIdentifier ?? "?")")
        if trusted {
            let pid = prev.processIdentifier
            waitForActive(prev, deadline: 0.5) { [weak self] success in
                guard let self, success else {
                    NSLog("ClipBar: paste — target never became active, skipping ⌘V")
                    return
                }
                self.deliverPaste(reversing: undoTarget, toPid: pid)
            }
        } else {
            notifyAccessibilityNeeded()
        }
    }

    /// Reverse the auto-paste captured in `snap` (if any) and then post ⌘V
    /// for the new item. Reversal is by character count when we know what
    /// text was pasted (universal: Cocoa, Electron contenteditables in
    /// Claude/Codex/Slack, TUIs running inside a terminal — a synthetic ⌘Z
    /// is unreliable in those contexts). Image-only or unknown content
    /// falls back to ⌘Z, which works in image-aware Cocoa apps.
    private func deliverPaste(reversing snap: AutoPasteSnapshot?, toPid pid: pid_t) {
        if let snap = snap, let pasted = snap.pastedText, !pasted.isEmpty {
            for _ in 0..<pasted.count {
                simulateKey(virtualKey: 0x33, toPid: pid)
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.06) { [weak self] in
                self?.simulateKey(virtualKey: 0x09, flags: .maskCommand, toPid: pid)
            }
        } else if snap != nil {
            simulateKey(virtualKey: 0x06, flags: .maskCommand, toPid: pid) // ⌘Z
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) { [weak self] in
                self?.simulateKey(virtualKey: 0x09, flags: .maskCommand, toPid: pid)
            }
        } else {
            simulateKey(virtualKey: 0x09, flags: .maskCommand, toPid: pid)
        }
    }

    private func waitForActive(_ target: NSRunningApplication,
                               deadline: TimeInterval,
                               completion: @escaping (Bool) -> Void) {
        let cutoff = Date().addingTimeInterval(deadline)
        func tick() {
            if target.isActive {
                // Tiny extra beat so the target's window is ready for keys.
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.04) {
                    completion(true)
                }
                return
            }
            if Date() >= cutoff {
                completion(false)
                return
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.015) { tick() }
        }
        tick()
    }

    /// Tell the user why paste failed and make sure ClipBar shows up in the
    /// Accessibility list with a single toggle to flip on.
    ///
    /// Calling AXIsProcessTrustedWithOptions(prompt:true) is what registers
    /// the bundle with TCC and adds the entry to the Accessibility pane. We
    /// fire it BEFORE the alert so by the time the user reaches Settings,
    /// the row is already there. Then we deep-link to the pane directly
    /// instead of relying on the API's redirect, which can no-op if Settings
    /// is already open with a cached list.
    private func notifyAccessibilityNeeded() {
        let opts = ["AXTrustedCheckOptionPrompt": true] as CFDictionary
        AXIsProcessTrustedWithOptions(opts)
        UserDefaults.standard.set(true, forKey: axPromptedKey)

        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Auto-paste needs Accessibility"
        alert.informativeText = "The item is on your clipboard — press ⌘V to paste it now.\n\nTo enable one-press paste, toggle ClipBar ON in System Settings → Privacy & Security → Accessibility. ClipBar will restart automatically once permission is granted."
        alert.addButton(withTitle: "Open System Settings")
        alert.addButton(withTitle: "Later")
        let response = alert.runModal()
        if response == .alertFirstButtonReturn {
            if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
                NSWorkspace.shared.open(url)
            }
        }
    }

    /// Write a history item onto the system pasteboard with our preferred
    /// representation: PNG bytes for image-aware targets *and* the absolute
    /// path as string fallback so text-only targets (terminal, browser bar)
    /// still get something useful. The suppress flag tells `clipd` not to
    /// echo this write back into history.
    private func writeClipboard(for item: ClipItem) -> Bool {
        let imageData = item.type == "image" ? ImageStore.read(item.contentHash, path: item.imagePath) : nil
        guard (item.type == "text" && item.content != nil) || imageData != nil else { return false }
        FileManager.default.createFile(atPath: suppressPath, contents: nil)
        let pb = NSPasteboard.general
        pb.clearContents()
        if item.type == "text", let text = item.content {
            pb.setString(text, forType: .string)
        } else if let path = item.imagePath, let data = imageData {
            pb.setData(data, forType: .png)
            pb.setString(path, forType: .string)
        }
        return true
    }

    /// Synthesize a keystroke. If `toPid` is given the event is delivered
    /// straight to that process and bypasses the global event chain — used
    /// for the ⌘V auto-paste so it doesn't loop back through our own hotkey.
    private func simulateKey(virtualKey: CGKeyCode,
                             flags: CGEventFlags = [],
                             toPid: pid_t? = nil) {
        // .privateState (not .combinedSessionState): macOS 15 prompts for Screen Recording
        // on combined-session sources because they expose cross-app input state.
        let src  = CGEventSource(stateID: .privateState)
        let down = CGEvent(keyboardEventSource: src, virtualKey: virtualKey, keyDown: true)
        let up   = CGEvent(keyboardEventSource: src, virtualKey: virtualKey, keyDown: false)
        if !flags.isEmpty {
            down?.flags = flags
            up?.flags   = flags
        }
        if let pid = toPid {
            down?.postToPid(pid)
            up?.postToPid(pid)
        } else {
            down?.post(tap: .cghidEventTap)
            up?.post(tap: .cghidEventTap)
        }
    }

    // MARK: Hotkey (Carbon — needs ZERO permissions)

    private func registerHotkey() {
        var spec = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind:  UInt32(kEventHotKeyPressed)
        )
        InstallEventHandler(
            GetApplicationEventTarget(),
            hotKeyHandler,
            1, &spec,
            Unmanaged.passUnretained(self).toOpaque(),
            nil
        )

        // 'CLBP' signature — any 4 bytes will do
        let sig: OSType = (OSType("C".utf8.first!) << 24) |
                          (OSType("L".utf8.first!) << 16) |
                          (OSType("B".utf8.first!) << 8)  |
                           OSType("P".utf8.first!)
        let key  = UInt32(kVK_ANSI_V)

        // ⌘⇧V — always registered.
        let shiftStatus = RegisterEventHotKey(
            key, UInt32(cmdKey | shiftKey),
            EventHotKeyID(signature: sig, id: 1),
            GetApplicationEventTarget(),
            0, &hotKeyRef
        )
        if shiftStatus != noErr {
            NSLog("ClipBar: RegisterEventHotKey ⌘⇧V failed (status=\(shiftStatus))")
        }

        // ⌘V — only when the user opted in AND Accessibility is granted.
        // Without AX we can't synthesize the paste, and we'd silently swallow
        // the user's normal ⌘V. So we leave it unregistered until trust is on.
        if autoPasteOnCmdV, AXIsProcessTrusted() {
            let plainStatus = RegisterEventHotKey(
                key, UInt32(cmdKey),
                EventHotKeyID(signature: sig, id: 2),
                GetApplicationEventTarget(),
                0, &cmdVHotKeyRef
            )
            if plainStatus != noErr {
                NSLog("ClipBar: RegisterEventHotKey ⌘V failed (status=\(plainStatus))")
            } else {
                NSLog("ClipBar: ⌘V intercept enabled")
            }
        }
    }
}
