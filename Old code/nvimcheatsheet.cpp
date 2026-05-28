// ============================================================
//                  NEOVIM COMMANDS CHEAT SHEET
//          Open this file in Neovim as a quick reference!
// ============================================================


// ─────────────────────────────────────────────
//                     MODES
// ─────────────────────────────────────────────
// Esc / Ctrl+C         → Go back to Normal mode (from any mode)
// i                    → Insert mode (before cursor)
// I                    → Insert mode (start of line)
// a                    → Insert mode (after cursor)
// A                    → Insert mode (end of line)
// o                    → New line below + Insert mode
// O                    → New line above + Insert mode
// v                    → Visual mode (character select)
// V                    → Visual Line mode (select whole lines)
// Ctrl+V               → Visual Block mode (column select)
// R                    → Replace mode (overwrites text as you type)
// :                    → Command mode


// ─────────────────────────────────────────────
//               SAVING & QUITTING
// ─────────────────────────────────────────────
// :w                   → Save (write)
// :w filename          → Save as new filename
// :wq  or  :x          → Save and quit
// :q                   → Quit
// :q!                  → Quit without saving (force)
// :qa                  → Quit all buffers
// :qa!                 → Quit all without saving


// ─────────────────────────────────────────────
//              CURSOR MOVEMENT (Normal mode)
// ─────────────────────────────────────────────
// h                    → Move left
// j                    → Move down
// k                    → Move up
// l                    → Move right
// w                    → Jump to start of next word
// W                    → Jump to next word (ignore punctuation)
// b                    → Jump back to start of previous word
// B                    → Jump back (ignore punctuation)
// e                    → Jump to end of current word
// 0                    → Beginning of line
// ^                    → First non-blank character of line
// $                    → End of line
// gg                   → Top of file
// G                    → Bottom of file
// 42G                  → Jump to line 42 (replace 42 with any number)
// Ctrl+D               → Scroll down half page
// Ctrl+U               → Scroll up half page
// Ctrl+F               → Scroll down full page
// Ctrl+B               → Scroll up full page
// zz                   → Center current line on screen
// zt                   → Current line to top of screen
// zb                   → Current line to bottom of screen
// H                    → Move cursor to top of screen
// M                    → Move cursor to middle of screen
// L                    → Move cursor to bottom of screen
// %                    → Jump to matching bracket ( ), [ ], { }
// {                    → Jump to previous empty line / code block
// }                    → Jump to next empty line / code block
// f<char>              → Find character forward on line (e.g. fa)
// F<char>              → Find character backward on line
// t<char>              → Move cursor just before character (forward)
// T<char>              → Move cursor just after character (backward)
// ;                    → Repeat last f/F/t/T
// ,                    → Repeat last f/F/t/T in reverse


// ─────────────────────────────────────────────
//                    EDITING
// ─────────────────────────────────────────────
// x                    → Delete character under cursor
// X                    → Delete character before cursor
// dd                   → Delete (cut) entire line
// D                    → Delete from cursor to end of line
// dw                   → Delete word
// d$                   → Delete to end of line
// d0                   → Delete to beginning of line
// 3dd                  → Delete 3 lines (replace 3 with any number)
// cc                   → Change (delete) entire line and enter Insert
// C                    → Change from cursor to end of line
// cw                   → Change word
// ciw                  → Change inner word (entire word under cursor)
// ci"                  → Change text inside quotes
// ci(                  → Change text inside parentheses
// r<char>              → Replace single character (e.g. ra replaces with a)
// u                    → Undo
// Ctrl+R               → Redo
// .                    → Repeat last change (very useful!)
// ~                    → Toggle case of character under cursor
// >>                   → Indent line
// <<                   → Unindent line
// ==                   → Auto-indent current line
// gg=G                 → Auto-indent entire file
// J                    → Join current line with line below
// Ctrl+A               → Increment number under cursor
// Ctrl+X               → Decrement number under cursor


// ─────────────────────────────────────────────
//                 COPY & PASTE
// ─────────────────────────────────────────────
// yy  or  Y            → Copy (yank) current line
// yw                   → Yank word
// y$                   → Yank to end of line
// 3yy                  → Yank 3 lines
// p                    → Paste after cursor / below line
// P                    → Paste before cursor / above line
// "ayy                 → Yank line into named register 'a'
// "ap                  → Paste from named register 'a'
// "+y                  → Yank to system clipboard
// "+p                  → Paste from system clipboard
//   NOTE: requires xclip on Arch → sudo pacman -S xclip
//   TIP: add  vim.opt.clipboard = "unnamedplus"  to options.lua
//        to make y/p always use system clipboard automatically


// ─────────────────────────────────────────────
//               VISUAL MODE COMMANDS
// ─────────────────────────────────────────────
// v                    → Start visual select
// V                    → Select whole lines
// Ctrl+V               → Block/column select
// y                    → Yank (copy) selected text
// d                    → Delete (cut) selected text
// c                    → Change selected text
// >                    → Indent selection
// <                    → Unindent selection
// ~                    → Toggle case of selection
// :s/old/new/g         → Search and replace in selection


// ─────────────────────────────────────────────
//              SEARCH & REPLACE
// ─────────────────────────────────────────────
// /word                → Search forward for "word"
// ?word                → Search backward for "word"
// n                    → Jump to next match
// N                    → Jump to previous match
// *                    → Search for word under cursor (forward)
// #                    → Search for word under cursor (backward)
// :%s/old/new/g        → Replace ALL occurrences in file
// :%s/old/new/gc       → Replace all with confirmation each time
// :s/old/new/g         → Replace in current line only
// :noh                 → Clear search highlight


// ─────────────────────────────────────────────
//           FILE & BUFFER MANAGEMENT
// ─────────────────────────────────────────────
// :e filename          → Open file in current buffer
// :ls  or  :buffers    → List all open buffers
// :bn                  → Go to next buffer
// :bp                  → Go to previous buffer
// :b filename          → Jump to buffer by name
// :bd                  → Close (delete) current buffer
// Shift+L              → Next buffer (LazyVim)
// Shift+H              → Previous buffer (LazyVim)
// :w                   → Save current file
// gf                   → Go to file whose name is under cursor


// ─────────────────────────────────────────────
//               SPLITS & WINDOWS
// ─────────────────────────────────────────────
// :vs filename         → Open file in vertical split
// :sp filename         → Open file in horizontal split
// Ctrl+W v             → Create vertical split (same file)
// Ctrl+W s             → Create horizontal split (same file)
// Ctrl+W h             → Move to split on the left
// Ctrl+W j             → Move to split below
// Ctrl+W k             → Move to split above
// Ctrl+W l             → Move to split on the right
// Ctrl+W =             → Make all splits equal size
// Ctrl+W q             → Close current split
// Ctrl+W o             → Close all splits except current


// ─────────────────────────────────────────────
//                     TABS
// ─────────────────────────────────────────────
// :tabnew filename     → Open file in new tab
// gt                   → Go to next tab
// gT                   → Go to previous tab
// :tabclose            → Close current tab
// :tabs                → List all tabs


// ─────────────────────────────────────────────
//           LAZYVIM SPECIFIC (Space = Leader)
// ─────────────────────────────────────────────
// Space+e              → Toggle file tree (neo-tree)
// Space+ff             → Fuzzy find files (Telescope)
// Space+fg             → Live grep (search text across all files)
// Space+fb             → Browse open buffers
// Space+/              → Search in current file
// Space+gg             → Open Lazygit
// Space+xx             → Toggle diagnostics list
// Space+ca             → Code actions (LSP)
// Space+cr             → Rename symbol (LSP)


// ─────────────────────────────────────────────
//                  LSP (Code Intel)
// ─────────────────────────────────────────────
// gd                   → Go to definition
// gD                   → Go to declaration
// gr                   → Find all references
// gI                   → Go to implementation
// K                    → Show hover documentation
// [d                   → Go to previous diagnostic
// ]d                   → Go to next diagnostic


// ─────────────────────────────────────────────
//              MARKS & JUMPS
// ─────────────────────────────────────────────
// m<letter>            → Set mark (e.g. ma sets mark 'a')
// '<letter>            → Jump to mark (e.g. 'a jumps to mark 'a')
// Ctrl+O               → Jump to previous location
// Ctrl+I               → Jump to next location
// ''                   → Jump back to last cursor position


// ─────────────────────────────────────────────
//                   MACROS
// ─────────────────────────────────────────────
// q<letter>            → Start recording macro (e.g. qa)
// q                    → Stop recording macro
// @<letter>            → Play macro (e.g. @a)
// @@                   → Repeat last macro
// 5@a                  → Play macro 'a' 5 times


// ─────────────────────────────────────────────
//                   TERMINAL
// ─────────────────────────────────────────────
// :terminal  or  :term → Open integrated terminal
// Ctrl+\ Ctrl+N        → Exit terminal back to Normal mode


// ─────────────────────────────────────────────
//                  MISC / USEFUL
// ─────────────────────────────────────────────
// :checkhealth         → Run Neovim diagnostics
// :noh                 → Clear search highlights
// Ctrl+Z               → WARNING: suspends Neovim to background (Unix)
//                        use 'fg' in terminal to get it back
// :help <topic>        → Open built-in help (e.g. :help dd)
// :version             → Show Neovim version info

