import QtQuick
import qs.Commons as Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "io.github.thibaudberdin.spotlight-files"

  property var manifest: null

  readonly property string pluginDir: (manifest && manifest.__sourceDir)
    ? String(manifest.__sourceDir).replace(/\/+$/, "")
    : decodeURIComponent(
        String(Qt.resolvedUrl(".")).replace(/^(file|qs):\/\//, "")
      ).replace(/\/+$/, "")

  Commons.Translations {
    id: strings
    directory: root.pluginDir + "/i18n"
  }

  function sanitizeTooltip(text) {
    return String(text || "").replace(/[<>&]/g, "")
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "\uf002"
    tooltipText: root.sanitizeTooltip(strings.t("bar.tooltip"))
    onPressed: function(pressedButton) {
      var targetId = root.bar && root.bar.pluginId ? root.bar.pluginId : root.moduleName
      var shellApi = root.bar ? root.bar.shell : null
      if (!shellApi) return
      if (pressedButton === Qt.RightButton) {
        if (shellApi.isPluginOpen(targetId)) shellApi.hide(targetId)
        else shellApi.summon(targetId, JSON.stringify({ mode: "content" }))
      } else {
        shellApi.toggle(targetId, JSON.stringify({ mode: "name" }))
      }
    }
  }
}
