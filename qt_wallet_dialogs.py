from flux_i18n import select_language, tr
from PyQt6 import QtCore, QtWidgets as W
from mainnet_electrum import SERVERS, server_endpoint
from wallet_keys import KeySet, own_account, public
from pathlib import Path
import json
from os.path import isreserved
import platform
import sys

import electrum
from PyQt6.QtCore import Qt, PYQT_VERSION_STR, qVersion
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                             QFormLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                             QRadioButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
                             QWizard, QWizardPage)
from desktop_wallet import Wallet, account_from_words
from flux_wallet import create_phrase, COIN


from flux_version import VERSION
FLUX_LOGO = Path(__file__).resolve().parent / "assets" / "flux.png"
ICONS = Path(electrum.__file__).parent / "gui" / "icons"
TITLE_BAR = (Qt.WindowType.CustomizeWindowHint | Qt.WindowType.WindowTitleHint | Qt.WindowType.WindowSystemMenuHint |
             Qt.WindowType.WindowMinMaxButtonsHint | Qt.WindowType.WindowCloseButtonHint)
DIALOG_FLAGS = Qt.WindowType.Dialog | TITLE_BAR


def icon(name):
    return QIcon(str(ICONS / name))


def show_startup_notice(settings, parent):
    setting = "startup/hideVersionReminder"
    if settings.value(setting, False, bool):
        return
    catalog = json.loads(FLUX_LOGO.with_name("startup-notice.json").read_text(encoding="utf-8"))
    languages = QtCore.QLocale.system().uiLanguages()
    language = select_language(languages or [QtCore.QLocale.system().name()], catalog)
    message, checkbox = catalog[language]
    box = QMessageBox(parent)
    box.setWindowTitle("Electrum-FLUX")
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setLayoutDirection(QtCore.QLocale(language).textDirection())
    box.setText(message)
    box.setIconPixmap(QIcon(str(FLUX_LOGO)).pixmap(48,48))
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    hide = QCheckBox(checkbox)
    box.setCheckBox(hide)
    box.exec()
    if hide.isChecked():
        settings.setValue(setting, True)
        settings.sync()


def password_input():
    box = QLineEdit()
    box.setEchoMode(QLineEdit.EchoMode.Password)
    return box


def info_table(parent, title, fields):
    dlg = QDialog(parent, DIALOG_FLAGS)
    dlg.setWindowTitle(title)
    form = QFormLayout(dlg)
    if title.startswith(tr("About")) or title == tr("Wallet information"):
        logo = QLabel()
        logo.setPixmap(QPixmap(str(FLUX_LOGO)).scaledToHeight(64, Qt.TransformationMode.SmoothTransformation))
        form.addRow(logo)
    for key, value in fields:
        label = QLabel(str(value))
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(key, label)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    buttons.rejected.connect(dlg.reject)
    form.addRow(buttons)
    dlg.setMinimumWidth(465)
    dlg.exec()


def about(parent):
    info_table(parent, tr("About Electrum-FLUX"), [
        (tr("Application"), f"Electrum-FLUX {VERSION} · {parent.wallet.network}"), (tr("Electrum components"), electrum.__version__),
        ("Qt", qVersion()), ("PyQt", PYQT_VERSION_STR), ("Python", platform.python_version()),
        (tr("System"), f"{platform.system()} {platform.release()} · {platform.machine()}"),
        (tr("Executable"), sys.executable),
    ])


def preferences(parent, settings):
    dlg = QDialog(parent, DIALOG_FLAGS)
    dlg.setWindowTitle(tr("Preferences"))
    layout = QVBoxLayout(dlg)
    tabs = QTabWidget()
    layout.addWidget(tabs)
    appearance = QWidget()
    form = QFormLayout(appearance)
    zeros = QSpinBox()
    zeros.setRange(0, 8)
    zeros.setValue(settings.value("zeros", 0, int))
    form.addRow(tr("Minimum decimal places"), zeros)
    alternating = QCheckBox(tr("Alternate table row colors"))
    alternating.setChecked(settings.value("alternating", True, bool))
    form.addRow(alternating)
    tabs.addTab(appearance, tr("Appearance"))
    network = QWidget()
    form = QFormLayout(network)
    interval = QSpinBox()
    interval.setRange(15, 600)
    interval.setSuffix(tr(" sec"))
    interval.setValue(settings.value("interval", 60, int))
    form.addRow(tr("Balance and history refresh interval"), interval)
    rate = QDoubleSpinBox()
    rate.setDecimals(8)
    rate.setRange(0.000001, 0.001)
    rate.setSingleStep(.000001)
    rate.setValue(settings.value("fee_rate", 1000, int) / COIN)
    form.addRow(tr("Fee rate (FLUX/kB)"), rate)
    tabs.addTab(network, tr("Transactions"))
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)
    if not dlg.exec():
        return False
    for key, value in (("zeros", zeros.value()), ("alternating", alternating.isChecked()),
                       ("interval", interval.value()), ("fee_rate", round(rate.value() * COIN))):
        settings.setValue(key, value)
    settings.sync()
    return True


class NetworkDialog(W.QDialog):
    def __init__(self, window):
        super().__init__(window, DIALOG_FLAGS)
        self.window = window
        self.setWindowTitle(tr("Network"))
        self.setMinimumWidth(480)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        layout = W.QVBoxLayout(self)
        status_box = W.QGroupBox("Flux mainnet")
        form = W.QFormLayout(status_box)
        coin_logo = QLabel()
        coin_logo.setPixmap(QIcon(str(FLUX_LOGO)).pixmap(36,36))
        form.addRow(coin_logo)
        self.state, self.peer, self.sync, self.error = (W.QLabel() for _ in range(4))
        for title, widget in ((tr("Connection status"), self.state), (tr("Server"), self.peer),
                              (tr("Last synchronized"), self.sync), (tr("Error"), self.error)):
            widget.setTextFormat(Qt.TextFormat.PlainText)
            widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            widget.setWordWrap(True)
            form.addRow(title, widget)
        layout.addWidget(status_box)
        controls = W.QGroupBox(tr("Server selection"))
        form = W.QFormLayout(controls)
        self.auto = W.QCheckBox(tr("Select server automatically"))
        self.auto.setChecked(window.connection.manual is None)
        form.addRow(self.auto)
        self.host = W.QComboBox()
        self.host.setEditable(True)
        self.host.addItems(SERVERS)
        self.port = W.QSpinBox()
        self.port.setRange(1, 65535)
        self.host.setCurrentText(window.settings.value("server/host", SERVERS[0], str))
        self.port.setValue(window.settings.value("server/port", 50001, int))
        self.host.activated.connect(lambda _: self.port.setValue(50001))
        form.addRow(tr("Server address"), self.host)
        form.addRow(tr("TCP port"), self.port)
        self.auto.toggled.connect(self.host.setDisabled)
        self.auto.toggled.connect(self.port.setDisabled)
        self.host.setDisabled(self.auto.isChecked())
        self.port.setDisabled(self.auto.isChecked())
        layout.addWidget(controls)
        buttons = W.QDialogButtonBox()
        self.apply_button = buttons.addButton(tr("Connect"), W.QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(tr("Close"), W.QDialogButtonBox.ButtonRole.RejectRole)
        self.apply_button.clicked.connect(self.apply)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update_status)
        self.timer.start(200)
        self.update_status()

    def update_status(self):
        state, peer, error = self.window.connection.status
        self.state.setText(state)
        self.peer.setText(peer or "—")
        self.sync.setText(self.window.last_sync or "—")
        self.error.setText((error or self.window.last_error or "—")[:600])
        self.apply_button.setEnabled(not self.window.busy)

    def apply(self):
        if self.window.busy:
            return
        try:
            manual = None if self.auto.isChecked() else server_endpoint(self.host.currentText(), self.port.value())
        except ValueError as exc:
            W.QMessageBox.warning(self, tr("Server address"), str(exc))
            return
        self.window.set_server(manual)
        self.update_status()


class WalletWizard(QWizard):
    def __init__(self, directory, selected=None, parent=None, *, network="mainnet"):
        self.network = network
        super().__init__(parent, DIALOG_FLAGS)
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.wallet = None
        self.setWindowTitle(tr("Electrum-FLUX — Wallet Wizard [{network}]").format(network=network))
        self.setWizardStyle(QWizard.WizardStyle.ClassicStyle)
        self.setPixmap(QWizard.WizardPixmap.LogoPixmap, QPixmap(str(FLUX_LOGO)).scaled(56, 56, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        for which, text in ((self.WizardButton.NextButton, tr("Next >")), (self.WizardButton.BackButton, tr("< Back")),
                            (self.WizardButton.FinishButton, tr("Open wallet")), (self.WizardButton.CancelButton, tr("Cancel"))):
            self.setButtonText(which, text)
        self.resize(590, 390)
        first = QWizardPage()
        first.setTitle(tr("Wallet file"))
        first.setSubTitle(tr("Select a saved wallet or enter a name for a new one."))
        form = QFormLayout(first)
        self.name = QComboBox()
        self.name.setEditable(True)
        files = (p for p in self.directory.iterdir() if p.is_file() and not p.name.endswith(".flux-lock")
                 and not (p.suffix[1:].isdigit() and p.stem.endswith(".tmp")))
        filenames = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
        self.name.addItems([p.name for p in filenames])
        self.name.setCurrentText(selected.name if selected else (filenames[0].name if filenames else "default_wallet"))
        form.addRow(tr("Network"), QLabel("Flux " + network))
        form.addRow(tr("Wallet"), self.name)
        form.addRow(tr("Folder"), QLabel(str(directory)))
        self.addPage(first)
        types = QWizardPage()
        types.setTitle(tr("Wallet type"))
        types.setSubTitle(tr("What kind of Flux wallet would you like to create?"))
        options = QVBoxLayout(types)
        self.standard = QRadioButton(tr("Standard wallet"))
        self.multi = QRadioButton(tr("Multi-signature wallet"))
        self.imported = QRadioButton(tr("Import Flux addresses or private keys"))
        self.standard.setChecked(True)
        for radio in (self.standard, self.multi, self.imported):
            options.addWidget(radio)
        counts = W.QWidget()
        line = W.QHBoxLayout(counts)
        self.required, self.total = QSpinBox(), QSpinBox()
        self.total.setRange(2,15)
        self.total.setValue(3)
        self.required.setRange(1,3)
        self.required.setValue(2)
        self.total.valueChanged.connect(self.required.setMaximum)
        line.addWidget(QLabel(tr("Signatures required")))
        line.addWidget(self.required)
        line.addWidget(QLabel(tr("of")))
        line.addWidget(self.total)
        line.addStretch()
        options.addWidget(counts)
        counts.setVisible(False)
        self.multi.toggled.connect(counts.setVisible)
        self.addPage(types)
        method = QWizardPage()
        method.setTitle(tr("Keystore"))
        method.setSubTitle(tr("Flux {network} wallet · BIP39").format(network=network))
        layout = QVBoxLayout(method)
        self.new = QRadioButton(tr("Create a new seed"))
        self.new.setChecked(True)
        layout.addWidget(self.new)
        self.restore = QRadioButton(tr("I already have a seed"))
        self.master = QRadioButton(tr("Use a master key"))
        for radio in (self.restore,self.master):
            layout.addWidget(radio)
        self.addPage(method)
        seed = QWizardPage()
        seed.setTitle(tr("Wallet seed phrase"))
        self.seedpage = seed
        layout = QVBoxLayout(seed)
        self.words = QPlainTextEdit()
        layout.addWidget(self.words)
        self.addPage(seed)
        cos = QWizardPage()
        cos.setTitle(tr("Cosigner keys"))
        self.cos_page = cos
        grid = QFormLayout(cos)
        self.own_pub = QLineEdit()
        self.own_pub.setReadOnly(True)
        self.cos = QPlainTextEdit()
        grid.addRow(tr("Your public key"), self.own_pub)
        grid.addRow(tr("Other cosigners’ public keys"), self.cos)
        self.addPage(cos)
        pw = QWizardPage()
        pw.setTitle(tr("Wallet password"))
        self.pwpage = pw
        form = QFormLayout(pw)
        self.pw, self.again = password_input(), password_input()
        form.addRow(tr("Password"), self.pw)
        self.repeat_label = QLabel(tr("Confirm password"))
        form.addRow(self.repeat_label, self.again)
        self.addPage(pw)
        self.currentIdChanged.connect(self.setup_page)
        self.generated = ""
        self.last_method = None

    def path(self):
        name = self.name.currentText().strip()
        if not name or name.endswith((".", " ")) or any(c in '<>:"/\\|?*' or ord(c) < 32 for c in name):
            raise ValueError(tr("Enter a valid file name."))
        path = self.directory / name
        if isreserved(name) or path.is_dir():
            raise ValueError(tr("Enter a valid file name."))
        legacy = self.directory / (name + ".fluxwallet")
        if not path.exists() and legacy.is_file():
            return legacy
        return path

    def mode(self):
        if self.imported.isChecked():
            return "import"
        if self.master.isChecked():
            return "master"
        return "create" if self.new.isChecked() else "restore"

    def key_document(self, cosigners=True):
        mode = self.mode()
        value = self.words.toPlainText().strip()
        if mode == "import":
            return {"kind":"import", "entries":value.split()}
        if mode in ("create","restore"):
            doc = {"words":" ".join(value.lower().split())}
        else:
            doc = {"master":value}
        if self.multi.isChecked() and cosigners:
            others = self.cos.toPlainText().split()
            if len(others) != self.total.value()-1:
                raise ValueError(tr("Enter {count} public keys for the other cosigners.").format(count=self.total.value() - 1))
            doc.update(kind="multisig", threshold=self.required.value(), cosigners=others)
        return doc

    def nextId(self):
        idx = self.currentId()
        if idx == 0:
            try:
                return 5 if self.path().is_file() else 1
            except ValueError:
                return 1
        if idx == 1 and self.imported.isChecked():
            return 3
        if idx == 3:
            return 4 if self.multi.isChecked() else 5
        return super().nextId()

    def setup_page(self, idx):
        if idx == 3:
            mode = self.mode()
            title, hint = {
                "import":(tr("Import addresses or keys"), tr("Enter FLUX addresses for a watch-only wallet or WIF private keys for signing. Enter one per line.")),
                "master":(tr("Master key"), tr("Enter an account xpub for a watch-only wallet or an xprv for signing. Branches /0 and /1 are derived from this key.")),
                "create":(tr("New seed phrase"), tr("Write down the words in order and keep them in a safe place.")),
                "restore":(tr("Restore wallet"), tr("Enter your BIP39 words in their original order.")),
            }[mode]
            self.seedpage.setTitle(title)
            self.seedpage.setSubTitle(hint)
            if mode != self.last_method:
                self.words.clear()
                self.last_method = mode
            self.words.setReadOnly(mode == "create")
            if mode=="create":
                self.generated = self.generated or create_phrase()
                self.words.setPlainText(self.generated)
        if idx == 4:
            self.own_pub.setText(public(own_account(self.key_document(False))).to_base58())
            self.cos_page.setSubTitle(tr("Share your public key with the other cosigners. Enter the other {count} public keys, one per line.").format(count=self.total.value() - 1))
        if idx == 5:
            exists = self.path().is_file()
            self.pwpage.setSubTitle(tr("Enter the password to open the wallet file.") if exists else tr("This password encrypts the wallet file on this computer."))
            self.again.setVisible(not exists)
            self.repeat_label.setVisible(not exists)
            self.pw.setFocus()

    def validateCurrentPage(self):
        try:
            path = self.path()
            idx = self.currentId()
            if idx in (3,4):
                KeySet(self.key_document(idx==4))
            if idx == 5:
                file_lock = QtCore.QLockFile(str(path.resolve()) + ".flux-lock")
                if not file_lock.tryLock(0):
                    raise ValueError(tr("This wallet file is already open in another window."))
                if path.is_file():
                    self.wallet = Wallet.open(path, self.pw.text(), self.network)
                else:
                    if self.pw.text() != self.again.text():
                        raise ValueError(tr("Passwords do not match."))
                    self.wallet = Wallet.create_with_keys(path,self.key_document(),self.pw.text(),self.network)
                self.wallet.file_lock = file_lock
            return True
        except Exception as exc:
            QMessageBox.warning(self, tr("Wallet"), str(exc))
            return False
