
## Setup Cursor on Ubuntu

For setting up Cursor on Ubuntu, use this repository:

https://github.com/hieutt192/Cursor-ubuntu/tree/main

## Keybindings / Keyboard Shortcuts (JSON)

```
// Place your key bindings in this file to override the defaults
[
    {
        "key": "cmd+1",
        "command": "chatgpt.addToThread"
    },
    {
        "key": "cmd+2",
        "command": "claude-vscode.insertAtMention",
        "when": "editorTextFocus"
    },    
    {
        "key": "cmd+3",
        "command": "geminicodeassist.terminal.addSelectionToChatContext",
        "when": "terminalTextSelectedInFocused"
    },
    {
        "key": "cmd+3",
        "command": "geminicodeassist.editor.addSelectionToChatContext",
        "when": "!terminalTextSelectedInFocused"
    },
    {
        "key": "cmd+4",
        "command": "openchamber.addToContext"
    },
    {
        "key": "cmd+5",
        "command": "openchamber.newSession"
    },
    {
        "key": "cmd+6",
        "command": "geminicodeassist.chat.new"
    },
    {
        "key": "cmd+8",
        "command": "cursor-openai-enabler.toggle"
    },
    {
        "key": "cmd+9",
        "command": "hunkwise.toggle"
    },
]

```
