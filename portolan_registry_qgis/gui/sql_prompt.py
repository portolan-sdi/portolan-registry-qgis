"""Ask before a saved project runs SQL that its author wrote."""

from __future__ import annotations

from qgis.gui import QgsCodeEditorSQL
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget

from portolan_registry_qgis.core.i18n import _

PROMPT_SIZE = QSize(560, 360)


class SqlPrompt(QDialog):
    """Show the full custom SQL of a saved layer, and ask whether to run it."""

    def __init__(self, layer_name: str, sql: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(_("Run SQL from this project?"))
        self.resize(PROMPT_SIZE)
        layout = QVBoxLayout(self)
        self.warning = QLabel(
            _(
                "The layer {name} runs SQL that the author of this project wrote. "
                "The SQL can read files on this computer and send them to other hosts. "
                "Run it only if you trust the project."
            ).format(name=layer_name)
        )
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)
        self.editor = QgsCodeEditorSQL()
        self.editor.setReadOnly(True)
        self.editor.setText(sql)
        layout.addWidget(self.editor, 1)
        self.buttons = QDialogButtonBox()
        self.run = self.buttons.addButton(_("Run SQL"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.keep = self.buttons.addButton(
            _("Keep empty layer"), QDialogButtonBox.ButtonRole.RejectRole
        )
        self.keep.setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)


def confirm_sql(layer_name: str, sql: str, parent: QWidget | None = None) -> bool:
    """Show the SQL of ``layer_name`` and return whether the user chose to run it."""
    prompt = SqlPrompt(layer_name, sql, parent)
    chosen: list[bool] = []
    prompt.accepted.connect(lambda: chosen.append(True))
    prompt.exec()
    return bool(chosen)
