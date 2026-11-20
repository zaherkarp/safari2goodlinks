-- export_all_windows.applescript
-- Reads every tab in every open Safari window.
--
-- Output (stdout, via osascript): plain text, three lines per tab:
--     <window index>
--     <title>
--     <url>
-- bin/stg2gl-triage (lib/collect.py) parses this into records.
--
-- Why not JSON like export_tabs.applescript: that script pipes the text
-- through `echo` in `do shell script`. On macOS /bin/sh is bash with
-- xpg_echo on, so `echo` expands backslash escapes such as "\n" inside a
-- page title, which could split one record into two. Returning the text
-- directly and letting Python split it avoids the shell entirely.
--
-- Only windows of the current Tab Group state are visible here: a window
-- shows one Tab Group (or no group) at a time. Tabs in groups that are not
-- open in any window come from the SafariTabs.db reader instead.

on run
	set out to {}
	tell application "Safari"
		set winIndex to 0
		repeat with w in (every window)
			set winIndex to winIndex + 1
			-- Some Safari windows (e.g. a closing window) have no tabs and
			-- raise an error when asked; skip them instead of aborting.
			try
				set tabList to tabs of w
			on error
				set tabList to {}
			end try
			repeat with t in tabList
				-- A blank tab returns `missing value` for URL (and sometimes
				-- name). Coerce to "" so the line count stays fixed.
				set rawTitle to ""
				set rawURL to ""
				try
					set rawTitle to name of t
					if rawTitle is missing value then set rawTitle to ""
				end try
				try
					set rawURL to URL of t
					if rawURL is missing value then set rawURL to ""
				end try
				-- SECURITY: strip CR/LF from both fields. A page controls its
				-- own <title>; an embedded newline would shift the line
				-- grouping and let a page inject an arbitrary URL record.
				set end of out to (winIndex as text)
				set end of out to my stripBreaks(rawTitle as text, " ")
				set end of out to my stripBreaks(rawURL as text, "")
			end repeat
		end repeat
	end tell

	set AppleScript's text item delimiters to linefeed
	set blob to out as text
	set AppleScript's text item delimiters to ""
	return blob
end run

-- Replace every linefeed and carriage return in s with r.
on stripBreaks(s, r)
	set s to my replaceChars(s, linefeed, r)
	set s to my replaceChars(s, return, r)
	return s
end stripBreaks

on replaceChars(sourceText, findChar, replaceWith)
	set AppleScript's text item delimiters to findChar
	set pieces to text items of sourceText
	set AppleScript's text item delimiters to replaceWith
	set cleaned to pieces as text
	set AppleScript's text item delimiters to ""
	return cleaned
end replaceChars
