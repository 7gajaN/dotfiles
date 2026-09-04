// Minimal black SDDM greeter.
//
// Deliberately bare: a solid black field, the username, and one password box.
// No clock, no avatars, no wallpaper, no power buttons. The only affordance
// beyond typing is F1, which cycles the session and briefly names it -- without
// that there is no way back to i3 short of editing /var/lib/sddm/state.conf.
//
// Authentication is handed straight to SDDM's own sddm.login(); this file never
// touches the password other than to pass it along.
//
// Two constraints come from the greeter that actually runs this. SDDM launches
// /usr/bin/sddm-greeter, which is linked against Qt5, NOT the Qt6
// sddm-greeter-qt6 binary sitting next to it:
//
//   * the import must carry an explicit version -- Qt5 rejects the versionless
//     `import QtQuick` form with "Library import requires a version";
//   * nothing from QtQuick.Controls may be used, because qt5-quickcontrols2 is
//     not installed, so TextField and friends cannot resolve. The password box
//     is therefore a plain QtQuick TextInput inside a Rectangle we draw.
//
// Test changes with:  sddm-greeter --test-mode --theme <dir>   (NOT the qt6 one)

import QtQuick 2.15

Rectangle {
    id: root

    // SDDM resizes the root item to the greeter window. These only matter for
    // `sddm-greeter --test-mode`.
    width: 1920
    height: 1080

    color: config.backgroundColor

    // RememberLastUser only populates lastUser after a first successful login,
    // so on a fresh install it is empty -- and sddm.login() with an empty user
    // always fails. Fall back to the first account userModel lists.
    property string fallbackUser: ""
    property string user: userModel.lastUser !== "" ? userModel.lastUser
                                                    : root.fallbackUser
    property int sessionIndex: sessionModel.lastIndex
    property bool busy: false

    Component.onCompleted: password.forceActiveFocus()

    Item {
        visible: false

        Repeater {
            model: userModel

            Item {
                required property string name
                required property int index

                Component.onCompleted: if (index === 0) root.fallbackUser = name
            }
        }
    }

    function tryLogin() {
        if (root.busy || password.text.length === 0)
            return
        root.busy = true
        sddm.login(root.user, password.text, root.sessionIndex)
    }

    Connections {
        target: sddm

        function onLoginFailed() {
            root.busy = false
            password.text = ""
            errorFlash.restart()
            password.forceActiveFocus()
        }
    }

    // Holds the error border colour for a moment after a bad password, so a
    // failure is visible without printing a message.
    Timer {
        id: errorFlash
        interval: 1200
        repeat: false
    }

    Text {
        id: userLabel

        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: passwordBox.top
        anchors.bottomMargin: 16

        text: root.user
        color: config.hintColor
        font.pixelSize: 13
    }

    Rectangle {
        id: passwordBox

        anchors.centerIn: parent
        width: 320
        height: 40

        color: "transparent"
        radius: 3
        border.width: 1
        border.color: errorFlash.running
                      ? config.borderColorError
                      : (password.activeFocus ? config.borderColorFocus
                                              : config.borderColor)

        Behavior on border.color { ColorAnimation { duration: 150 } }

        TextInput {
            id: password

            anchors.fill: parent
            anchors.leftMargin: 12
            anchors.rightMargin: 12

            verticalAlignment: TextInput.AlignVCenter
            horizontalAlignment: TextInput.AlignHCenter

            echoMode: TextInput.Password
            passwordCharacter: "•"
            passwordMaskDelay: 0
            selectByMouse: true
            clip: true

            color: config.textColor
            font.pixelSize: 16
            enabled: !root.busy
            opacity: root.busy ? 0.4 : 1.0

            onAccepted: root.tryLogin()

            Keys.onPressed: function (event) {
                if (event.key === Qt.Key_F1) {
                    root.sessionIndex = (root.sessionIndex + 1) % sessionRepeater.count
                    sessionHint.show()
                    event.accepted = true
                }
            }
        }
    }

    // Only visible for a couple of seconds after F1. The name is rendered by
    // whichever delegate matches the current index, so no JavaScript ever has
    // to reach into sessionModel's roles.
    Item {
        id: sessionHint

        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: passwordBox.bottom
        anchors.topMargin: 18
        width: parent.width
        height: 16

        opacity: 0

        function show() {
            opacity = 1
            hideTimer.restart()
        }

        Behavior on opacity { NumberAnimation { duration: 200 } }

        Repeater {
            id: sessionRepeater
            model: sessionModel

            Text {
                required property string name
                required property int index

                anchors.horizontalCenter: parent.horizontalCenter
                visible: index === root.sessionIndex
                text: name
                color: config.hintColor
                font.pixelSize: 12
            }
        }

        Timer {
            id: hideTimer
            interval: 2000
            onTriggered: sessionHint.opacity = 0
        }
    }
}
