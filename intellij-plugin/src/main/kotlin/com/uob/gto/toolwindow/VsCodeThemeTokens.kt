package com.uob.gto.toolwindow

import com.intellij.ui.JBColor
import com.intellij.util.ui.UIUtil
import java.awt.Color

/** Maps the current IntelliJ theme onto the `--vscode-*` CSS custom
 * property names shared/report-view's CSS is written against — see that
 * package's README.md "Styling contract" section. VS Code supplies these
 * automatically; a non-VS-Code host (this one) has to generate them itself. */
object VsCodeThemeTokens {

    private fun hex(c: Color): String = String.format("#%02x%02x%02x", c.red, c.green, c.blue)

    fun cssBlock(): String {
        val fg = hex(UIUtil.getLabelForeground())
        val bg = hex(UIUtil.getPanelBackground())
        val editorBg = hex(UIUtil.getTextFieldBackground())
        val border = hex(JBColor.border())
        val hoverBg = hex(UIUtil.getListSelectionBackground(false))
        val link = hex(JBColor(0x2470B3, 0x589DF6))
        val error = hex(JBColor(0xC94F4F, 0xE55765))
        val warn = hex(JBColor(0x9E6C1F, 0xD9A544))
        val good = hex(JBColor(0x3E8039, 0x59A869))
        val codeBg = hex(UIUtil.getDecoratedRowColor())
        val fontFamily = UIUtil.getLabelFont().family
        val monoFamily = com.intellij.util.ui.JBFont.create(java.awt.Font(java.awt.Font.MONOSPACED, java.awt.Font.PLAIN, 12)).family

        return """
            :root {
              --vscode-foreground: $fg;
              --vscode-descriptionForeground: $fg;
              --vscode-errorForeground: $error;
              --vscode-font-family: "$fontFamily", sans-serif;
              --vscode-editor-font-family: "$monoFamily", monospace;
              --vscode-editor-background: $editorBg;
              --vscode-panel-border: $border;
              --vscode-list-hoverBackground: $hoverBg;
              --vscode-textLink-foreground: $link;
              --vscode-textCodeBlock-background: $codeBg;
              --vscode-editorWarning-foreground: $warn;
              --vscode-testing-iconPassed: $good;
              --vscode-testing-iconFailed: $error;
              --vscode-gitDecoration-addedResourceForeground: $good;
              --vscode-gitDecoration-deletedResourceForeground: $error;
              --vscode-button-border: $border;
              --vscode-button-secondaryBackground: $bg;
              --vscode-button-secondaryForeground: $fg;
              --vscode-button-secondaryHoverBackground: $hoverBg;
            }
        """.trimIndent()
    }
}
