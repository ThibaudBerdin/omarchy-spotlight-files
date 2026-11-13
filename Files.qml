import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

Item {
  id: root

  property string omarchyPath: Quickshell.env("OMARCHY_PATH")
  property var shell: null
  property var manifest: null

  // Resolve plugin directory whether loaded via qs:/// or file:/// or injected via manifest.__sourceDir
  readonly property string pluginDir: (manifest && manifest.__sourceDir)
    ? String(manifest.__sourceDir).replace(/\/+$/, "")
    : decodeURIComponent(String(Qt.resolvedUrl(".")).replace(/^(file|qs):\/\//, "")).replace(/\/+$/, "")
  readonly property string helperScript: pluginDir + "/search.py"

  Commons.Translations {
    id: strings
    directory: root.pluginDir + "/i18n"
  }

  property bool opened: false
  property string searchMode: "name"
  property string effectiveMode: "name"
  property string filterText: ""
  onFilterTextChanged: if (filterInput.text !== filterText) filterInput.text = filterText

  property int selectedIndex: 0
  property bool cursorActive: true
  property bool isRecent: true
  property bool searching: false
  property int cachedDocs: 0

  // Plain value snapshot of currently selected row (immune to ListModel clear/invalidate)
  property string selectedPath: ""
  property string selectedName: ""
  property bool selectedIsDir: false
  property bool selectedInDropbox: false

  property bool indexing: indexProc.running
  property int indexDone: 0
  property int indexTotal: 0

  property string previewKind: ""
  property string previewImageUrl: ""
  property string previewText: ""
  property string previewMeta: ""
  property bool previewInDropbox: false
  property bool previewLoading: false

  // Visual tokens matching Omarchy [menu] surface
  property color background: Commons.Color.menu.background
  property color foreground: Commons.Color.menu.text
  property color border: Commons.Color.menu.border
  property var borderSpec: Border.surfaceSpec("menu", "border", border, Math.max(1, Style.space(2)))
  property color scrim: Commons.Color.menu.scrim
  property color selectedBackground: Commons.Color.menu.selectedBackground
  property color selectedText: Commons.Color.menu.selectedText
  readonly property int cornerRadius: Style.cornerRadius
  property string fontFamily: Style.font.menuFamily
  property int contentMargin: Style.spacing.panelPadding
  property int headerHeight: Math.max(Style.space(38), Style.font.title + Style.spacing.controlPaddingY * 2)
  property int footerHeight: Math.max(Style.space(26), Style.font.caption + Style.space(8))
  property int contentSpacing: Style.spacing.md
  property int cardWidth: Math.min(Style.space(960), panel.width - Style.gapsOut * 2)
  property int cardHeight: Math.min(Style.space(620), panel.height - Style.gapsOut * 2)
  property int rowHeight: Math.max(Style.space(54), Style.font.body + Style.font.caption + Style.spacing.rowPaddingX * 2)
  property int snippetRowHeight: Math.max(Style.space(68), Style.font.body + Style.font.caption * 2 + Style.spacing.rowPaddingX * 2)

  function syncSelection() {
    if (root.cursorActive && displayModel.count > 0 && root.selectedIndex >= 0 && root.selectedIndex < displayModel.count) {
      var r = displayModel.get(root.selectedIndex)
      root.selectedPath = String(r.path || "")
      root.selectedName = String(r.name || "")
      root.selectedIsDir = r.isDir === true
      root.selectedInDropbox = r.inDropbox === true
    } else {
      root.selectedPath = ""
      root.selectedName = ""
      root.selectedIsDir = false
      root.selectedInDropbox = false
    }
  }

  function open(payloadJson) {
    var payload = ({})
    try { payload = JSON.parse(payloadJson || "{}") } catch (e) { payload = ({}) }

    root.opened = true
    if (payload.mode === "content" || payload.mode === "name") {
      root.searchMode = payload.mode
    }
    root.filterText = ""
    filterInput.text = ""
    root.selectedIndex = 0
    root.cursorActive = true
    root.disarmPointer()

    if (!refreshProc.running) {
      refreshProc.running = true
    }
    root.scheduleQuery(true)
    Qt.callLater(function() { filterInput.forceActiveFocus() })
  }

  function close() {
    queryTimer.stop()
    previewTimer.stop()
    root.opened = false
  }

  function toggle() {
    if (root.opened) root.close()
    else root.open("{}")
  }

  function disarmPointer() {
    pointerGate.reset()
  }

  function selectFromPointer(index, item, mouse) {
    if (!pointerGate.moved(item, mouse)) return
    if (root.selectedIndex !== index || !root.cursorActive) {
      root.cursorActive = true
      root.selectedIndex = index
      root.syncSelection()
      root.schedulePreview()
    }
  }

  function setFilter(nextText) {
    if (root.filterText === nextText) return
    root.filterText = nextText
    root.selectedIndex = 0
    root.cursorActive = true
    root.disarmPointer()
    root.scheduleQuery(false)
  }

  function toggleMode() {
    root.searchMode = root.searchMode === "name" ? "content" : "name"
    root.selectedIndex = 0
    root.cursorActive = true
    root.disarmPointer()
    root.scheduleQuery(true)
  }

  function toggleIndex() {
    if (indexProc.running) {
      indexProc.running = false
    } else {
      root.indexDone = 0
      root.indexTotal = 0
      indexProc.running = true
    }
  }

  function select(delta) {
    if (displayModel.count === 0) return
    root.disarmPointer()
    if (!root.cursorActive) {
      root.cursorActive = true
      root.selectedIndex = delta < 0 ? displayModel.count - 1 : 0
    } else {
      root.selectedIndex = (root.selectedIndex + delta + displayModel.count) % displayModel.count
    }
    root.syncSelection()
    resultList.positionViewAtIndex(root.selectedIndex, ListView.Contain)
    root.schedulePreview()
  }

  function selectAbsolute(index) {
    if (displayModel.count === 0) return
    root.disarmPointer()
    root.cursorActive = true
    root.selectedIndex = Math.max(0, Math.min(index, displayModel.count - 1))
    root.syncSelection()
    resultList.positionViewAtIndex(root.selectedIndex, ListView.Contain)
    root.schedulePreview()
  }

  function activateSelected(revealInFolder) {
    var p = root.selectedPath
    if (!p) return
    root.close()
    var kind = revealInFolder ? "reveal" : "open"
    Util.execArgv(["python3", root.helperScript, "action", kind, "--", p])
  }

  function copySelectedPath() {
    var p = root.selectedPath
    if (!p) return
    root.close()
    Util.execArgv(["python3", root.helperScript, "action", "copy-path", "--", p])
  }

  function copyDropboxLink() {
    var p = root.selectedPath
    if (!p || !root.selectedInDropbox) return
    root.close()
    Util.execArgv(["python3", root.helperScript, "action", "dropbox-link", "--", p])
  }

  function trashSelected() {
    var p = root.selectedPath
    if (!p) return
    trashProc.targetPath = p
    trashProc.command = ["python3", root.helperScript, "action", "trash", "--", p]
    trashProc.running = true
  }

  // ---------------- Query orchestration ----------------

  property int querySerial: 0
  property var pendingQuerySpec: null

  function scheduleQuery(immediate) {
    queryTimer.stop()
    if (immediate || !root.filterText.trim()) {
      root.dispatchQuery()
    } else {
      var wantContent = root.searchMode === "content" || /^\s*(\?|c:)/i.test(root.filterText)
      queryTimer.interval = wantContent ? 130 : 60
      queryTimer.start()
    }
  }

  function dispatchQuery() {
    root.querySerial += 1
    root.pendingQuerySpec = {
      serial: root.querySerial,
      mode: root.searchMode,
      query: root.filterText
    }
    root.searching = true
    if (queryProc.running) {
      queryProc.running = false
    } else {
      queryProc.startPending()
    }
  }

  Timer {
    id: queryTimer
    repeat: false
    onTriggered: root.dispatchQuery()
  }

  Process {
    id: queryProc
    property int activeSerial: 0
    property string collected: ""

    function startPending() {
      if (!root.pendingQuerySpec) {
        root.searching = false
        return
      }
      var spec = root.pendingQuerySpec
      root.pendingQuerySpec = null
      activeSerial = spec.serial
      collected = ""
      command = [
        "python3",
        root.helperScript,
        "query",
        "--mode=" + spec.mode,
        "--limit=55",
        "--text=" + spec.query
      ]
      running = true
    }

    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: queryProc.collected = text
    }

    onExited: function(exitCode) {
      if (root.pendingQuerySpec) {
        startPending()
        return
      }
      root.searching = false
      if (exitCode !== 0 || activeSerial !== root.querySerial) return
      root.applyQueryResponse(collected)
    }
  }

  function applyQueryResponse(rawJson) {
    var payload = null
    try { payload = JSON.parse(rawJson) } catch (e) { payload = null }
    if (!payload) return

    root.effectiveMode = payload.mode || root.searchMode
    root.isRecent = payload.recent === true
    if (typeof payload.cachedDocs === "number" && payload.cachedDocs > 0) {
      root.cachedDocs = payload.cachedDocs
    }

    var rows = Array.isArray(payload.results) ? payload.results : []
    displayModel.clear()
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i]
      displayModel.append({
        path: String(r.path || ""),
        name: String(r.name || ""),
        dir: String(r.dir || ""),
        glyph: String(r.glyph || "󰈔"),
        isDir: r.isDir === true,
        inDropbox: r.inDropbox === true,
        snippet: String(r.snippet || "")
      })
    }

    if (displayModel.count === 0) {
      root.selectedIndex = 0
      root.syncSelection()
      root.clearPreview()
    } else {
      if (root.selectedIndex >= displayModel.count) root.selectedIndex = displayModel.count - 1
      if (root.selectedIndex < 0) root.selectedIndex = 0
      root.syncSelection()
      Qt.callLater(function() {
        if (displayModel.count > 0) resultList.positionViewAtIndex(root.selectedIndex, ListView.Contain)
      })
      root.schedulePreview()
    }
  }

  // ---------------- Preview orchestration ----------------

  property int previewSerial: 0
  property var pendingPreviewSpec: null

  function clearPreview() {
    root.previewKind = ""
    root.previewImageUrl = ""
    root.previewText = ""
    root.previewMeta = ""
    root.previewInDropbox = false
    root.previewLoading = false
    previewFlickable.contentY = 0
  }

  function schedulePreview() {
    previewTimer.stop()
    if (!root.selectedPath) {
      root.clearPreview()
      return
    }
    previewTimer.start()
  }

  Timer {
    id: previewTimer
    interval: 75
    repeat: false
    onTriggered: {
      var p = root.selectedPath
      if (!p) {
        root.clearPreview()
        return
      }
      root.previewSerial += 1
      root.pendingPreviewSpec = {
        serial: root.previewSerial,
        path: p,
        query: root.effectiveMode === "content" ? root.filterText : ""
      }
      root.previewLoading = true
      if (previewProc.running) {
        previewProc.running = false
      } else {
        previewProc.startPending()
      }
    }
  }

  Process {
    id: previewProc
    property int activeSerial: 0
    property string collected: ""

    function startPending() {
      if (!root.pendingPreviewSpec) {
        root.previewLoading = false
        return
      }
      var spec = root.pendingPreviewSpec
      root.pendingPreviewSpec = null
      activeSerial = spec.serial
      collected = ""
      command = [
        "python3",
        root.helperScript,
        "preview",
        "--query=" + spec.query,
        "--",
        spec.path
      ]
      running = true
    }

    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: previewProc.collected = text
    }

    onExited: function(exitCode) {
      if (root.pendingPreviewSpec) {
        startPending()
        return
      }
      root.previewLoading = false
      if (exitCode !== 0 || activeSerial !== root.previewSerial) return
      var data = null
      try { data = JSON.parse(collected) } catch (e) { data = null }
      if (!data) return
      previewFlickable.contentY = 0
      root.previewKind = String(data.kind || "")
      root.previewImageUrl = data.imagePath ? Util.fileUrl(String(data.imagePath)) : ""
      root.previewText = String(data.text || "")
      root.previewMeta = String(data.meta || "")
      root.previewInDropbox = data.inDropbox === true
    }
  }

  // ---------------- Background cache refresh & on-demand indexing ----------------

  Process {
    id: refreshProc
    property string collected: ""
    command: ["python3", root.helperScript, "refresh-paths"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: refreshProc.collected = text
    }
    onExited: function(exitCode) {
      if (exitCode === 0 && collected.trim()) {
        try {
          var st = JSON.parse(collected)
          if (typeof st.cachedDocs === "number") root.cachedDocs = st.cachedDocs
        } catch (e) {}
      }
      if (root.opened && !root.filterText.trim() && displayModel.count === 0) {
        root.scheduleQuery(true)
      }
    }
  }

  Process {
    id: indexProc
    command: ["python3", root.helperScript, "index"]
    stdout: SplitParser {
      onRead: function(line) {
        try {
          var p = JSON.parse(line)
          if (typeof p.done === "number") root.indexDone = p.done
          if (typeof p.total === "number") root.indexTotal = p.total
          if (typeof p.done === "number") root.cachedDocs = p.done
        } catch (e) {}
      }
    }
    onExited: {
      if (root.opened && root.effectiveMode === "content" && root.filterText.trim()) {
        root.scheduleQuery(true)
      }
    }
  }

  Process {
    id: trashProc
    property string targetPath: ""
    onExited: function(exitCode) {
      if (exitCode === 0) {
        if (!refreshProc.running) refreshProc.running = true
        root.scheduleQuery(true)
      }
    }
  }

  ListModel { id: displayModel }

  PointerMoveGate {
    id: pointerGate
    referenceItem: card
  }

  // ---------------- UI Overlay ----------------

  OverlayWindow {
    id: panel
    shown: root.opened
    WlrLayershell.namespace: "thibaud-files"

    Rectangle {
      anchors.fill: parent
      color: root.scrim
    }

    MouseArea {
      anchors.fill: parent
      onClicked: root.close()
    }

    BorderSurface {
      id: card
      width: root.cardWidth
      height: root.cardHeight
      radius: root.cornerRadius
      anchors.centerIn: parent
      color: root.background
      borderSpec: root.borderSpec
      padding: root.contentMargin

      MouseArea {
        anchors.fill: parent
        onClicked: filterInput.forceActiveFocus()
      }

      Item {
        id: keyCatcher
        anchors.fill: parent

        Keys.onPressed: function(event) {
          var ctrl = (event.modifiers & Qt.ControlModifier) !== 0
          var shift = (event.modifiers & Qt.ShiftModifier) !== 0

          if (event.key === Qt.Key_Escape) {
            if (root.filterText) root.setFilter("")
            else root.close()
            event.accepted = true
          } else if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab || (ctrl && event.key === Qt.Key_G)) {
            root.toggleMode()
            event.accepted = true
          } else if (ctrl && event.key === Qt.Key_I) {
            root.toggleIndex()
            event.accepted = true
          } else if (ctrl && event.key === Qt.Key_Y) {
            root.copySelectedPath()
            event.accepted = true
          } else if (ctrl && event.key === Qt.Key_L) {
            root.copyDropboxLink()
            event.accepted = true
          } else if (ctrl && event.key === Qt.Key_Delete) {
            root.trashSelected()
            event.accepted = true
          } else if (Util.editsFilter(event, root.filterText)) {
            root.setFilter(Util.editedFilter(event, root.filterText))
            event.accepted = true
          } else if (event.key === Qt.Key_Up || (ctrl && (event.key === Qt.Key_P || event.key === Qt.Key_K))) {
            root.select(-1)
            event.accepted = true
          } else if (event.key === Qt.Key_Down || (ctrl && (event.key === Qt.Key_N || event.key === Qt.Key_J))) {
            root.select(1)
            event.accepted = true
          } else if (event.key === Qt.Key_PageUp) {
            root.select(-7)
            event.accepted = true
          } else if (event.key === Qt.Key_PageDown) {
            root.select(7)
            event.accepted = true
          } else if (event.key === Qt.Key_Home && ctrl) {
            root.selectAbsolute(0)
            event.accepted = true
          } else if (event.key === Qt.Key_End && ctrl) {
            root.selectAbsolute(displayModel.count - 1)
            event.accepted = true
          } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            if (shift && root.selectedIsDir && root.selectedPath) {
              var rel = root.selectedPath
              var homePrefix = Quickshell.env("HOME") + "/"
              if (rel.indexOf(homePrefix) === 0) rel = rel.slice(homePrefix.length)
              root.setFilter(rel + "/")
            } else {
              root.activateSelected(ctrl)
            }
            event.accepted = true
          }
        }
      }

      Column {
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset
        spacing: root.contentSpacing

        // Header bar: Mode toggle pill + FilterInput + Status indicator
        Item {
          width: parent.width
          height: root.headerHeight

          Rectangle {
            id: modePill
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            height: Math.max(Style.space(28), Style.font.body + Style.space(10))
            width: modeRow.implicitWidth + Style.space(18)
            radius: root.cornerRadius
            color: root.effectiveMode === "content" ? root.selectedBackground : Util.alpha(root.foreground, 0.1)

            Row {
              id: modeRow
              anchors.centerIn: parent
              spacing: Style.space(6)

              Text {
                textFormat: Text.PlainText
                text: root.effectiveMode === "content" ? "󰈙" : "󰈞"
                color: root.effectiveMode === "content" ? root.selectedText : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                anchors.verticalCenter: parent.verticalCenter
              }

              Text {
                textFormat: Text.PlainText
                text: root.effectiveMode === "content" ? strings.t("mode.content") : strings.t("mode.name")
                color: root.effectiveMode === "content" ? root.selectedText : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
                anchors.verticalCenter: parent.verticalCenter
              }
            }

            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: {
                root.toggleMode()
                filterInput.forceActiveFocus()
              }
            }
          }

          Item {
            anchors.left: modePill.right
            anchors.leftMargin: Style.space(12)
            anchors.right: statusText.left
            anchors.rightMargin: Style.space(12)
            anchors.top: parent.top
            anchors.bottom: parent.bottom

            FilterInput {
              id: filterInput
              anchors.fill: parent
              verticalAlignment: Text.AlignVCenter
              keyHandler: keyCatcher
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.heading
              onTextChanged: if (text !== root.filterText) root.setFilter(text)
            }

            Text {
              textFormat: Text.PlainText
              anchors.fill: filterInput
              verticalAlignment: Text.AlignVCenter
              visible: !root.filterText && !filterInput.preeditText
              text: root.searchMode === "content"
                ? strings.t("search.placeholderContent")
                : strings.t("search.placeholderName")
              color: root.foreground
              opacity: 0.5
              font.family: root.fontFamily
              font.pixelSize: Style.font.heading
              elide: Text.ElideRight
            }
          }

          Text {
            id: statusText
            textFormat: Text.PlainText
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            color: root.foreground
            opacity: 0.62
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            text: {
              if (root.indexing && root.indexTotal > 0) {
                var pct = Math.round((root.indexDone * 100) / Math.max(1, root.indexTotal))
                return strings.t("status.indexing")
                  .arg(Commons.I18n.formatNumber(root.indexDone))
                  .arg(Commons.I18n.formatNumber(root.indexTotal))
                  .arg(Commons.I18n.formatNumber(pct))
              }
              if (root.searching) return strings.t("status.searching")
              if (root.isRecent) return strings.t("status.recent")
              var countStr = strings.tn("status.results", displayModel.count).arg(Commons.I18n.formatNumber(displayModel.count))
              if (root.effectiveMode === "content" && root.cachedDocs > 0) {
                return countStr + " · " + strings.t("status.cachedDocs").arg(Commons.I18n.formatNumber(root.cachedDocs))
              }
              return countStr
            }
          }
        }

        Rectangle {
          width: parent.width
          height: Style.normalBorderWidth
          color: Util.alpha(root.border, 0.25)
        }

        // Main split area: Results list (left) + Live Preview (right)
        // 5 children in Column => 4 spacings (root.contentSpacing * 4)
        Item {
          width: parent.width
          height: Math.max(0, parent.height - root.headerHeight - root.footerHeight - root.contentSpacing * 4 - Style.normalBorderWidth * 2)

          Row {
            anchors.fill: parent
            spacing: 0
            visible: displayModel.count > 0

            // Left pane: Results list
            Item {
              width: Math.round(parent.width * 0.54)
              height: parent.height
              clip: true

              ListView {
                id: resultList
                anchors.fill: parent
                anchors.rightMargin: root.contentMargin
                model: displayModel
                clip: true
                spacing: Style.space(4)
                boundsBehavior: Flickable.StopAtBounds

                delegate: Rectangle {
                  id: rowItem
                  required property int index
                  required property string path
                  required property string name
                  required property string dir
                  required property string glyph
                  required property bool isDir
                  required property bool inDropbox
                  required property string snippet

                  readonly property bool hasCursor: root.cursorActive && index === root.selectedIndex

                  width: ListView.view.width
                  height: snippet.length > 0 ? root.snippetRowHeight : root.rowHeight
                  radius: root.cornerRadius
                  color: hasCursor ? root.selectedBackground : "transparent"

                  Row {
                    anchors.fill: parent
                    anchors.leftMargin: Style.space(12)
                    anchors.rightMargin: Style.space(12)
                    anchors.topMargin: Style.space(6)
                    anchors.bottomMargin: Style.space(6)
                    spacing: Style.space(10)

                    Text {
                      textFormat: Text.PlainText
                      text: rowItem.glyph
                      color: rowItem.hasCursor ? root.selectedText : root.foreground
                      opacity: rowItem.hasCursor ? 1.0 : 0.82
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.heading
                      anchors.verticalCenter: parent.verticalCenter
                      width: Style.space(22)
                      horizontalAlignment: Text.AlignHCenter
                    }

                    Column {
                      width: parent.width - Style.space(32)
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: Style.space(2)

                      Row {
                        width: parent.width
                        spacing: Style.space(6)

                        Text {
                          textFormat: Text.PlainText
                          text: rowItem.name
                          color: rowItem.hasCursor ? root.selectedText : root.foreground
                          font.family: root.fontFamily
                          font.pixelSize: Style.font.title
                          font.bold: rowItem.hasCursor
                          elide: Text.ElideMiddle
                          width: parent.width - (rowItem.inDropbox ? Style.space(20) : 0)
                        }

                        Text {
                          visible: rowItem.inDropbox
                          textFormat: Text.PlainText
                          text: "󰇣"
                          color: rowItem.hasCursor ? root.selectedText : root.foreground
                          opacity: 0.65
                          font.family: root.fontFamily
                          font.pixelSize: Style.font.caption
                          anchors.verticalCenter: parent.verticalCenter
                        }
                      }

                      Text {
                        textFormat: Text.PlainText
                        text: rowItem.dir
                        color: rowItem.hasCursor ? root.selectedText : root.foreground
                        opacity: rowItem.hasCursor ? 0.82 : 0.56
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                        elide: Text.ElideLeft
                        width: parent.width
                      }

                      Text {
                        visible: rowItem.snippet.length > 0
                        textFormat: Text.PlainText
                        text: rowItem.snippet
                        color: rowItem.hasCursor ? root.selectedText : root.foreground
                        opacity: rowItem.hasCursor ? 0.92 : 0.72
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                        elide: Text.ElideRight
                        width: parent.width
                      }
                    }
                  }

                  MouseArea {
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onPositionChanged: function(mouse) {
                      root.selectFromPointer(rowItem.index, rowItem, mouse)
                    }
                    onClicked: function(mouse) {
                      root.cursorActive = true
                      root.selectedIndex = rowItem.index
                      root.syncSelection()
                      root.activateSelected((mouse.modifiers & Qt.ControlModifier) !== 0)
                    }
                  }
                }
              }
            }

            // Right pane: Live Preview
            Item {
              width: parent.width - Math.round(parent.width * 0.54)
              height: parent.height
              clip: true

              Rectangle {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: Style.normalBorderWidth
                color: Util.alpha(root.border, 0.25)
              }

              Column {
                id: previewColumn
                anchors.fill: parent
                anchors.leftMargin: root.contentMargin
                spacing: Style.space(8)

                // Preview header: filename + metadata
                Column {
                  id: previewHeader
                  width: parent.width
                  spacing: Style.space(2)

                  Text {
                    textFormat: Text.PlainText
                    text: root.selectedName
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.title
                    font.bold: true
                    elide: Text.ElideMiddle
                    width: parent.width
                  }

                  Text {
                    textFormat: Text.PlainText
                    visible: root.previewMeta.length > 0
                    text: root.previewMeta
                    color: root.foreground
                    opacity: 0.6
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                    width: parent.width
                  }
                }

                Rectangle {
                  width: parent.width
                  height: Style.normalBorderWidth
                  color: Util.alpha(root.border, 0.18)
                }

                // Preview body
                Item {
                  width: parent.width
                  height: Math.max(0, parent.height - previewHeader.height - Style.normalBorderWidth - previewColumn.spacing * 2)
                  clip: true

                  SizedImage {
                    id: imgPreview
                    visible: root.previewImageUrl.length > 0
                    width: parent.width
                    height: root.previewText.length > 0 ? Math.round(parent.height * 0.65) : parent.height
                    url: root.previewImageUrl
                    verticalAlignment: Image.AlignTop
                    asynchronous: true
                    smooth: true
                  }

                  Flickable {
                    id: previewFlickable
                    visible: root.previewText.length > 0
                    anchors.top: root.previewImageUrl.length > 0 ? imgPreview.bottom : parent.top
                    anchors.topMargin: root.previewImageUrl.length > 0 ? Style.space(8) : 0
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    contentWidth: width
                    contentHeight: previewBodyText.paintedHeight
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds

                    Text {
                      id: previewBodyText
                      textFormat: Text.PlainText
                      width: parent.width
                      text: root.previewText
                      color: root.foreground
                      opacity: 0.88
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                      wrapMode: Text.Wrap
                    }
                  }

                  Text {
                    textFormat: Text.PlainText
                    visible: !root.previewLoading && root.previewImageUrl.length === 0 && root.previewText.length === 0
                    anchors.centerIn: parent
                    text: root.previewKind === "folder" ? strings.t("preview.emptyFolder") : strings.t("preview.noText")
                    color: root.foreground
                    opacity: 0.45
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                  }
                }
              }
            }
          }

          // Empty state
          Column {
            anchors.centerIn: parent
            width: parent.width - 2 * root.contentMargin
            spacing: Style.space(10)
            visible: displayModel.count === 0 && !root.searching

            Text {
              textFormat: Text.PlainText
              text: root.effectiveMode === "content" ? "󰈙" : "󰈞"
              color: root.foreground
              opacity: 0.45
              font.family: root.fontFamily
              font.pixelSize: Style.font.displayLarge
              horizontalAlignment: Text.AlignHCenter
              anchors.horizontalCenter: parent.horizontalCenter
            }

            Text {
              textFormat: Text.PlainText
              text: root.filterText.trim()
                ? strings.t("empty.noMatches").arg(root.filterText)
                : strings.t("empty.noRecent")
              color: root.foreground
              opacity: 0.72
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              horizontalAlignment: Text.AlignHCenter
              width: parent.width
              elide: Text.ElideRight
            }

            Text {
              textFormat: Text.PlainText
              visible: root.effectiveMode === "content" && root.cachedDocs < 500
              text: strings.t("empty.contentHint")
              color: root.foreground
              opacity: 0.5
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              horizontalAlignment: Text.AlignHCenter
              width: parent.width
              elide: Text.ElideRight
            }
          }
        }

        Rectangle {
          width: parent.width
          height: Style.normalBorderWidth
          color: Util.alpha(root.border, 0.25)
        }

        // Footer shortcut bar
        Row {
          width: parent.width
          height: root.footerHeight
          spacing: Style.space(16)
          clip: true

          Text {
            textFormat: Text.PlainText
            text: strings.t("footer.open")
            color: root.foreground
            opacity: 0.62
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            anchors.verticalCenter: parent.verticalCenter
          }

          Text {
            textFormat: Text.PlainText
            text: strings.t("footer.reveal")
            color: root.foreground
            opacity: 0.62
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            anchors.verticalCenter: parent.verticalCenter
          }

          Text {
            textFormat: Text.PlainText
            text: strings.t("footer.mode")
            color: root.foreground
            opacity: 0.62
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            anchors.verticalCenter: parent.verticalCenter
          }

          Text {
            textFormat: Text.PlainText
            text: strings.t("footer.copy")
            color: root.foreground
            opacity: 0.62
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            anchors.verticalCenter: parent.verticalCenter
          }

          Text {
            visible: root.selectedInDropbox
            textFormat: Text.PlainText
            text: strings.t("footer.dropbox")
            color: root.foreground
            opacity: 0.78
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            anchors.verticalCenter: parent.verticalCenter
          }

          Text {
            textFormat: Text.PlainText
            text: root.indexing ? strings.t("footer.stopIndex") : strings.t("footer.index")
            color: root.foreground
            opacity: root.indexing ? 0.9 : 0.62
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            anchors.verticalCenter: parent.verticalCenter
          }
        }
      }
    }
  }
}
