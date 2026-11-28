-- export_tabs.applescript
-- Reads every tab in Safari's front window.
--
-- Output (stdout, via osascript): plain text, two lines per tab:
--     <title>
--     <url>
-- bin/stg2gl parses this with stg2gl_core.parse_front_tab_lines.
--
-- Why plain text and not JSON: this script used to build JSON by piping the
-- text through `echo` in `do shell script`. On macOS /bin/sh is bash with
-- xpg_echo on, so `echo` expands backslash escapes inside a page title. A
-- page whose <title> contained the two characters "\n" could split its
-- record and add a URL of its choosing to the save list, and "\c" silently
-- dropped every tab after it. Returning the text directly, as
-- export_all_windows.applescript does, never touches a shell.

on run
	set out to {}
	tell application "Safari"
		if (count of windows) is 0 then return ""
		-- A window with no tabs (e.g. one that is closing) raises an error
		-- when asked; treat it as empty instead of aborting.
		try
			set tabList to tabs of front window
		on error
			set tabList to {}
		end try
		repeat with t in tabList
			-- A blank tab returns `missing value`; coerce to "".
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
			-- Skip tabs with no URL: there is nothing to save, and an empty
			-- last line would be lost when the shell captures the output with
			-- $(...) (it strips trailing newlines), breaking the pairing.
			if (rawURL as text) is not "" then
				-- SECURITY: strip CR/LF from both fields. A page controls its
				-- own <title>; an embedded newline would shift the pairing and
				-- let a page inject an arbitrary URL record.
				set end of out to my stripBreaks(rawTitle as text, " ")
				set end of out to my stripBreaks(rawURL as text, "")
			end if
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
