#!/bin/bash
# Opens the flight tracker dashboard in Chrome, reusing an existing tab if found.
osascript <<'EOF'
tell application "Google Chrome"
  activate
  set dashUrl to "file:///Users/emmalynchen/projects/flight-tracker/index.html"
  set found to false
  repeat with w in windows
    set tabIndex to 0
    repeat with t in tabs of w
      set tabIndex to tabIndex + 1
      if URL of t contains "flight-tracker" then
        set active tab index of w to tabIndex
        set URL of t to dashUrl
        set found to true
        exit repeat
      end if
    end repeat
    if found then exit repeat
  end repeat
  if not found then
    tell window 1 to make new tab with properties {URL:dashUrl}
  end if
end tell
EOF
