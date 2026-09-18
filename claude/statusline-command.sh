#!/usr/bin/env bash
# Claude Code status line script, parses the JSON on stdin without jq
# The installer copies this file to ~/.claude/statusline-command.sh and points
# statusLine in ~/.claude/settings.json at it. Weather is opt-in through
# ~/.claude/local/statusline.conf with STATUSLINE_CITY, STATUSLINE_LAT and
# STATUSLINE_LON. The local folder is never overwritten by an install.

input=$(cat)

for field in cwd model used_pct cost total_in total_out fh_pct fh_reset wk_pct wk_reset; do
  IFS= read -r "$field"
done <<< "$(awk '
  function field(text, key, value) {
    if (!match(text, "\"" key "\"[[:space:]]*:[[:space:]]*[^,}]*")) return ""
    value = substr(text, RSTART, RLENGTH)
    sub(/^.*:[[:space:]]*/, "", value)
    gsub(/"/, "", value)
    sub(/^[[:space:]]*/, "", value)
    sub(/[[:space:]]*$/, "", value)
    return value
  }
  function outer(text, key) {
    if (!match(text, "\"" key "\"[[:space:]]*:[[:space:]]*{")) return ""
    return substr(text, RSTART)
  }
  function block(text, key) {
    if (!match(text, "\"" key "\"[[:space:]]*:[[:space:]]*{[^}]*}")) return ""
    return substr(text, RSTART, RLENGTH)
  }
  { input = input $0 "\n" }
  END {
    context = outer(input, "context_window")
    limits = outer(input, "rate_limits")
    five_hour = block(limits, "five_hour")
    seven_day = block(limits, "seven_day")
    print field(input, "current_dir")
    print field(input, "display_name")
    print field(context, "used_percentage")
    print field(input, "total_cost_usd")
    print field(context, "total_input_tokens")
    print field(context, "total_output_tokens")
    print field(five_hour, "used_percentage")
    print field(five_hour, "resets_at")
    print field(seven_day, "used_percentage")
    print field(seven_day, "resets_at")
  }
' <<< "$input")"

[ -n "${STATUSLINE_DEBUG:-}" ] && echo "$input" > "${HOME}/.claude/statusline-input-debug.json"

DIR_C='\033[1;38;5;75m'
MODEL_C='\033[38;5;244m'
COST_C='\033[38;5;108m'
TOKEN_C='\033[38;5;146m'
WEATHER_C='\033[38;5;110m'
DIM_C='\033[38;5;240m'
RESET='\033[0m'

pct_color() {
  local p=${1:-0}
  p=${p%.*}
  [ -z "$p" ] && p=0
  if   [ "$p" -lt 50 ]; then echo '\033[38;5;114m'
  elif [ "$p" -lt 75 ]; then echo '\033[38;5;179m'
  elif [ "$p" -lt 90 ]; then echo '\033[38;5;208m'
  else                       echo '\033[1;38;5;203m'
  fi
}

dir_display=$(basename "$cwd")

if [ -n "$used_pct" ] && [ "$used_pct" != "null" ]; then
  used_int=${used_pct%.*}
  bar_width=10
  filled=$(( used_int * bar_width / 100 ))
  empty=$(( bar_width - filled ))
  fill_chars=""; empty_chars=""
  for ((i=0; i<filled; i++)); do fill_chars+="█"; done
  for ((i=0; i<empty;  i++)); do empty_chars+="░"; done
  ctx_color=$(pct_color "$used_int")
  ctx_str="${ctx_color}${fill_chars}${DIM_C}${empty_chars}${ctx_color} ${used_int}%${RESET}"
else
  ctx_str="${DIM_C}░░░░░░░░░░ --%${RESET}"
fi

if [ -n "$cost" ] && [ "$cost" != "null" ]; then
  cost_str=$(printf '$%.2f' "$cost")
else
  cost_str='$0.00'
fi

# Tokens, current session only (reset to 0 when a new session starts via /clear)
token_str=""
if [ -n "$total_in" ] && [ "$total_in" != "null" ] \
   && [ -n "$total_out" ] && [ "$total_out" != "null" ]; then
  in_k=$(awk  "BEGIN { printf \"%.1f\", $total_in  / 1000 }")
  out_k=$(awk "BEGIN { printf \"%.1f\", $total_out / 1000 }")
  token_str="↑${in_k}K ↓${out_k}K"
fi

fmt_until() {
  local target=$1
  [ -z "$target" ] || [ "$target" = "null" ] && return
  local diff=$(( target - $(date +%s) ))
  [ "$diff" -le 0 ] && { echo "now"; return; }
  local h=$(( diff / 3600 ))
  local m=$(( (diff % 3600) / 60 ))
  if [ "$h" -ge 24 ]; then
    local d=$(( h / 24 )); h=$(( h % 24 ))
    echo "${d}d${h}h"
  elif [ "$h" -gt 0 ]; then
    echo "${h}h${m}m"
  else
    echo "${m}m"
  fi
}

limit_str=""
if [ -n "$fh_pct" ] && [ "$fh_pct" != "null" ]; then
  fh_until=$(fmt_until "$fh_reset")
  fh_color=$(pct_color "$fh_pct")
  part="${fh_color}5h:${fh_pct%.*}%"
  [ -n "$fh_until" ] && part="${part} ${DIM_C}(${fh_until})"
  part="${part}${RESET}"
  limit_str="$part"
fi
if [ -n "$wk_pct" ] && [ "$wk_pct" != "null" ]; then
  wk_until=$(fmt_until "$wk_reset")
  wk_color=$(pct_color "$wk_pct")
  part="${wk_color}wk:${wk_pct%.*}%"
  [ -n "$wk_until" ] && part="${part} ${DIM_C}(${wk_until})"
  part="${part}${RESET}"
  if [ -n "$limit_str" ]; then
    limit_str="${limit_str} ${DIM_C}·${RESET} ${part}"
  else
    limit_str="$part"
  fi
fi

read_statusline_config() {
  grep "^$1=" "${HOME}/.claude/local/statusline.conf" | tail -1 \
    | sed 's/^[^=]*=//;s/^[[:space:]]*//;s/[[:space:]]*$//' \
    | sed "s/^\([\"']\)\(.*\)\1$/\2/" \
    | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

weather_str=""
if [ -f "${HOME}/.claude/local/statusline.conf" ]; then
  city=$(read_statusline_config STATUSLINE_CITY)
  lat=$(read_statusline_config STATUSLINE_LAT)
  lon=$(read_statusline_config STATUSLINE_LON)
  if [ -n "$city" ] && [[ "$lat" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] && [[ "$lon" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
    WEATHER_CACHE_DIR="${HOME}/.claude/weather-cache"
    CACHE_FILE="${WEATHER_CACHE_DIR}/openmeteo-${lat}_${lon}.json"
    CACHE_TTL=1800
    mkdir -p "$WEATHER_CACHE_DIR"
    now=$(date +%s)

    weather_refresh=1
    for freshness_file in "$CACHE_FILE" "${CACHE_FILE}.failed"; do
      if [ -f "$freshness_file" ]; then
        mtime=$(stat -c %Y "$freshness_file" 2>/dev/null || stat -f %m "$freshness_file" 2>/dev/null || echo 0)
        age=$(( now - mtime ))
        if [ "$age" -lt "$CACHE_TTL" ]; then
          weather_refresh=0
          break
        fi
      fi
    done
    if [ "$weather_refresh" -eq 1 ]; then
      if curl -sf -m 5 \
        "https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&current=temperature_2m,weather_code&daily=temperature_2m_max&hourly=precipitation&forecast_days=1&timezone=UTC" \
        -o "${CACHE_FILE}.tmp" 2>/dev/null \
        && mv "${CACHE_FILE}.tmp" "$CACHE_FILE"; then
        rm -f "${CACHE_FILE}.failed"
      else
        rm -f "${CACHE_FILE}.tmp"
        : > "${CACHE_FILE}.failed"
      fi
    fi

    if [ -f "$CACHE_FILE" ]; then
      for field in temp_int high_int cond rain_in; do
        IFS= read -r "$field"
      done <<< "$(awk -v hour="$(date -u +%H)" '
        function last_number(text, key, pattern, value) {
          pattern = "\"" key "\":[0-9.-]*"
          while (match(text, pattern)) {
            value = substr(text, RSTART, RLENGTH)
            text = substr(text, RSTART + RLENGTH)
            sub(/^[^:]*:/, "", value)
          }
          return value
        }
        function array_values(text, key, value) {
          if (!match(text, "\"" key "\":\\[[^]]*\\]")) return ""
          value = substr(text, RSTART, RLENGTH)
          sub(/^[^[]*\[/, "", value)
          sub(/\]$/, "", value)
          return value
        }
        function rounded(value) {
          if (value == "") return ""
          value += 0
          return value < 0 ? int(value - 0.5) : int(value + 0.5)
        }
        function condition(code) {
          if (code == "") return ""
          code += 0
          if (code == 0) return "Clear"
          if (code >= 1 && code <= 3) return "Cloudy"
          if (code >= 45 && code <= 48) return "Fog"
          if (code >= 51 && code <= 55) return "Drizzle"
          if (code >= 56 && code <= 57) return "FreezDriz"
          if (code >= 61 && code <= 65) return "Rain"
          if (code >= 66 && code <= 67) return "FreezRain"
          if (code >= 71 && code <= 77) return "Snow"
          if (code >= 80 && code <= 82) return "Showers"
          if (code >= 85 && code <= 86) return "SnowShwr"
          if (code >= 95 && code <= 99) return "Storm"
          return "?"
        }
        function rain_phrase(values, count, precipitation, i, offset) {
          count = split(values, precipitation, ",")
          hour += 0
          for (i = hour + 1; i <= count; i++) {
            if (precipitation[i] + 0 > 0) {
              offset = i - 1 - hour
              if (offset == 0) return "rain now"
              if (offset == 1) return "rain in 1 hour"
              return "rain in " offset " hours"
            }
          }
          return ""
        }
        { input = input $0 "\n" }
        END {
          high = array_values(input, "temperature_2m_max")
          sub(/,.*/, "", high)
          print rounded(last_number(input, "temperature_2m"))
          print rounded(high)
          print condition(last_number(input, "weather_code"))
          print rain_phrase(array_values(input, "precipitation"))
        }
      ' "$CACHE_FILE")"

      if [ -n "$temp_int" ] && [ -n "$cond" ]; then
        if [ -n "$high_int" ]; then
          weather_str="${city} ${cond} ${temp_int}C / ${high_int}C"
        else
          weather_str="${city} ${cond} ${temp_int}C"
        fi
      fi

      if [ -n "$rain_in" ]; then
        if [ -n "$weather_str" ]; then
          weather_str="${weather_str} ${rain_in}"
        else
          weather_str="$rain_in"
        fi
      fi
    fi
  fi
fi

line="${DIR_C}${dir_display}${RESET}  ${MODEL_C}${model}${RESET}  ${ctx_str}  ${COST_C}${cost_str}${RESET}"
[ -n "$token_str"   ] && line="${line}  ${TOKEN_C}${token_str}${RESET}"
[ -n "$limit_str"   ] && line="${line}  ${limit_str}"
[ -n "$weather_str" ] && line="${line}  ${WEATHER_C}${weather_str}${RESET}"

printf "%b" "$line"
