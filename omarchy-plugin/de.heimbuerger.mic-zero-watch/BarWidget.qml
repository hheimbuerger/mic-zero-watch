import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui

// Bar switch for mic-zero-watch's hands-free dictation mode.
//
// The mic's color is the switch: alert color while the mode is on and able to
// dictate, grey while it's off or overruled by an open Meet tab. A small badge
// in the corner shows the substate.
//
// The service publishes what it is doing in $XDG_RUNTIME_DIR/mic-zero-watch/
// status.json and removes the file when it stops; a click runs
// `mic-zero-watch --dictation toggle`, which the service picks up.
BarWidget {
  id: root
  moduleName: "de.heimbuerger.mic-zero-watch"

  readonly property string statusPath: Quickshell.env("XDG_RUNTIME_DIR") + "/mic-zero-watch/status.json"

  property var status: null
  readonly property bool running: status !== null
  readonly property bool micConnected: running && status.connected === true
  readonly property bool enabled: running && status.dictation === true
  readonly property bool pausedForMeet: enabled && status.meet === true
  readonly property bool armed: enabled && !pausedForMeet
  readonly property bool recording: armed && status.recording === true

  // Gap between the mic and its badge (negative: the mic button reserves a
  // square slot with empty space beside the glyph), and extra room after the
  // badge so it doesn't crowd the next widget (the clock) more than its other
  // neighbours.
  readonly property int badgeGap: -6
  readonly property int trailingGap: 3

  implicitWidth: button.implicitWidth + (badge.visible ? badgeGap + badge.implicitWidth : 0) + trailingGap
  implicitHeight: button.implicitHeight

  function click(b) {
    // Right-click opens voxtype's settings, standing in for Omarchy's own
    // dictation indicator, which can be hidden next to this widget.
    if (b === Qt.RightButton) root.bar.run("omarchy-voxtype-config")
    else if (root.micConnected) root.bar.run("mic-zero-watch --dictation toggle")
  }

  function parse(text) {
    try {
      root.status = JSON.parse(text)
    } catch (error) {
      root.status = null
    }
  }

  FileView {
    id: statusFile
    path: root.statusPath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.parse(text())
    onLoadFailed: root.status = null
  }

  // The service replaces the file atomically, which a file watch can lose
  // track of; re-reading once a second keeps the switch honest regardless.
  Timer {
    interval: 1000
    running: true
    repeat: true
    onTriggered: statusFile.reload()
  }

  BarIconButton {
    id: button
    anchors.left: parent.left
    anchors.top: parent.top
    anchors.bottom: parent.bottom
    width: implicitWidth
    bar: root.bar
    text: root.micConnected ? "󰍬" : "󰍭"
    active: root.armed
    dimmed: !root.armed
    tooltipText: !root.running ? "mic-zero-watch isn't running"
      : !root.micConnected ? "Hands-free dictation off: the wireless mic isn't connected"
      : !root.enabled ? "Hands-free dictation off (click to switch on, right-click for voxtype settings)"
      : root.pausedForMeet ? "Hands-free dictation on, but paused while Meet is open"
      : root.recording ? "Hands-free dictation: recording (mute the mic to paste)"
      : "Hands-free dictation armed: unmute the mic to dictate (click to switch off)"
    onPressed: function(b) { root.click(b) }
  }

  // Substate badge next to the mic, after a DAW's record-arm: hollow circle =
  // armed (the next unmute starts dictating), filled circle = recording,
  // video = overruled by Meet. (Without a mic the service switches the mode
  // off, so there is no "on but no mic" state to show.)
  Text {
    id: badge
    anchors.left: button.right
    anchors.leftMargin: root.badgeGap
    anchors.baseline: parent.verticalCenter
    anchors.baselineOffset: Math.round(button.fontSize * 0.45)
    visible: text !== ""
    text: root.pausedForMeet ? "󰕧"
      : root.recording ? "󰑊"
      : root.armed ? "󰝦"
      : ""
    font.family: button.fontFamily
    font.pixelSize: Math.round(button.fontSize * 0.75)
    color: button.active ? button.activeColor : button.foreground
    opacity: button.opacity

    MouseArea {
      anchors.fill: parent
      acceptedButtons: Qt.LeftButton | Qt.RightButton
      cursorShape: Qt.PointingHandCursor
      onPressed: function(mouse) { root.click(mouse.button) }
    }
  }
}
