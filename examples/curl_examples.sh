#!/usr/bin/env bash
# 冷链追责服务 调用示例（curl）。
# 用法：BASE=http://127.0.0.1:8000 bash examples/curl_examples.sh
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"
C="COLD-DEMO-1"

echo "1) 创建货物：编号 / 起始时间 / 温度上限 / 起始保管方"
curl -sS -X POST "$BASE/cargo" -H 'Content-Type: application/json' -d "{
  \"cargo_id\": \"$C\",
  \"started_at\": \"2026-09-30T08:00:00+08:00\",
  \"temp_upper_limit\": \"4.0\",
  \"initial_holder\": \"北京冷库\"
}"; echo

echo "2) 测温（10:00，8.2℃ > 上限 4℃，开启越温区间）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-temp-1\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T10:00:00+08:00\", \"temperature\": \"8.2\"
}"; echo

echo "3) 交接（12:00，北京冷库 -> 上海冷库），区间在此刻拆分"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-hand-1\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T12:00:00+08:00\",
  \"giver\": \"北京冷库\", \"receiver\": \"上海冷库\"
}"; echo

echo "4) 交接（14:00，上海冷库 -> 广州冷库）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-hand-2\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T14:00:00+08:00\",
  \"giver\": \"上海冷库\", \"receiver\": \"广州冷库\"
}"; echo

echo "5) 测温（16:00，3.1℃ <= 上限，闭合越温区间）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-temp-2\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T16:00:00+08:00\", \"temperature\": \"3.1\"
}"; echo

echo "6) 幂等重传第 2) 步（相同内容返回 200 replayed，不重复写入）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-temp-1\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T10:00:00+08:00\", \"temperature\": \"8.2\"
}"; echo

echo "7) 追责报告：越温起止 + 各责任方片段（按时间）"
curl -sS "$BASE/cargo/$C/report"; echo

echo "8) 责任确认视图（仅已闭合区间）：区间=开启测温事件ID，含快照标识与汇总状态"
curl -sS "$BASE/cargo/$C/confirmations"; echo
# 按开启测温事件ID定位单条区间；快照由区间起止与责任片段决定
SNAP="$(curl -sS "$BASE/cargo/$C/confirmations/evt-temp-1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["snapshot_id"])')"
echo "   当前快照标识：$SNAP"

echo "9) 北京冷库（区间起始责任方）确认"
curl -sS -X POST "$BASE/cargo/$C/stances" -H 'Content-Type: application/json' -d "{
  \"stance_id\": \"st-beijing-1\", \"cargo_id\": \"$C\",
  \"interval_id\": \"evt-temp-1\", \"snapshot_id\": \"$SNAP\",
  \"responsible_party\": \"北京冷库\", \"stance\": \"confirm\"
}"; echo

echo "10) 上海冷库提出异议（异议必须给理由）"
curl -sS -X POST "$BASE/cargo/$C/stances" -H 'Content-Type: application/json' -d "{
  \"stance_id\": \"st-shanghai-1\", \"cargo_id\": \"$C\",
  \"interval_id\": \"evt-temp-1\", \"snapshot_id\": \"$SNAP\",
  \"responsible_party\": \"上海冷库\", \"stance\": \"object\",
  \"reason\": \"交接时车厢温度正常，疑似前序制冷故障\"
}"; echo

echo "11) 上海冷库改口确认（必须换新表态编号；历次表态保留）"
curl -sS -X POST "$BASE/cargo/$C/stances" -H 'Content-Type: application/json' -d "{
  \"stance_id\": \"st-shanghai-2\", \"cargo_id\": \"$C\",
  \"interval_id\": \"evt-temp-1\", \"snapshot_id\": \"$SNAP\",
  \"responsible_party\": \"上海冷库\", \"stance\": \"confirm\"
}"; echo

echo "12) 广州冷库确认后汇总状态应为 all_confirmed"
curl -sS -X POST "$BASE/cargo/$C/stances" -H 'Content-Type: application/json' -d "{
  \"stance_id\": \"st-guangzhou-1\", \"cargo_id\": \"$C\",
  \"interval_id\": \"evt-temp-1\", \"snapshot_id\": \"$SNAP\",
  \"responsible_party\": \"广州冷库\", \"stance\": \"confirm\"
}"; echo
curl -sS "$BASE/cargo/$C/confirmations/evt-temp-1"; echo

echo "13) 表态编号同内容重传 -> 200 replayed（返回原记录，不重复写入）"
curl -sS -X POST "$BASE/cargo/$C/stances" -H 'Content-Type: application/json' -d "{
  \"stance_id\": \"st-beijing-1\", \"cargo_id\": \"$C\",
  \"interval_id\": \"evt-temp-1\", \"snapshot_id\": \"$SNAP\",
  \"responsible_party\": \"北京冷库\", \"stance\": \"confirm\"
}"; echo

echo "14) 表态历史（历次全保留；晚到事件导致快照变化后 is_current=false 仍可查）"
curl -sS "$BASE/cargo/$C/stances/history/evt-temp-1"; echo
