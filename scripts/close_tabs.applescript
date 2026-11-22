-- close_tabs.applescript
-- Close every tab, in every Safari window, whose URL is in the argument list.
--
-- Arguments: one URL key per argument, as made by url_key() in
-- lib/stg2gl_core.py: the URL without its #fragment, with "(" and ")"
-- percent-encoded. This script builds the same key from each tab's URL,
-- so a tab matches even if the page has since scrolled to a #heading.
--
-- Output: the number of tabs closed.
--
-- Only tabs in open windows on this Mac can be closed. Tabs in Tab Groups
-- that are not open, and tabs on an iPhone or iPad, are out of reach.

on run argv
	set targets to {}
	repeat with a in argv
		set end of targets to (a as text)
	end repeat
	if (count of targets) is 0 then return "0"

	set closedCount to 0
	tell application "Safari"
		-- Capture window ids first. Closing a window's last tab closes the
		-- window, which would shift positional references like "window 2".
		set winIds to id of every window
		repeat with wid in winIds
			try
				set w to window id wid
				set n to count of tabs of w
				-- Walk backwards so closing tab i does not renumber the
				-- tabs still to be checked.
				repeat with i from n to 1 by -1
					try
						set u to URL of tab i of w
						if u is not missing value then
							set k to my urlKey(u as text)
							considering case
								set hit to (targets contains k)
							end considering
							if hit then
								close tab i of w
								set closedCount to closedCount + 1
							end if
						end if
					end try
				end repeat
			end try
		end repeat
	end tell
	return closedCount as text
end run

-- Same normalisation as url_key() in lib/stg2gl_core.py.
on urlKey(u)
	set AppleScript's text item delimiters to "#"
	set base to text item 1 of u
	set AppleScript's text item delimiters to ""
	set base to my replaceChars(base, "(", "%28")
	set base to my replaceChars(base, ")", "%29")
	return base
end urlKey

on replaceChars(sourceText, findChar, replaceWith)
	set AppleScript's text item delimiters to findChar
	set pieces to text items of sourceText
	set AppleScript's text item delimiters to replaceWith
	set cleaned to pieces as text
	set AppleScript's text item delimiters to ""
	return cleaned
end replaceChars
