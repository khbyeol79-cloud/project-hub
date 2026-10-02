"""Read-only collection status: python -m bot.status [--json]."""
import argparse
import json

if __package__:
    from . import database
else:
    import database


def format_status(snapshot):
    totals = snapshot["totals"]
    lines = ["Project Hub 수집 상태", "시각: UTC (한국 시간은 +9시간)",
             f"업로드 완료 {totals.get('uploaded', 0)}건 | 대기 {totals.get('pending', 0)}건",
             f"실패·재시도 {totals.get('failed', 0)}건 | 과거 기록 확인 필요 {totals.get('needs_review', 0)}건"]
    for item in snapshot["channels"]:
        history = item["history"] or {}
        lines.extend([
            f"채널 {item['channel_id']} ({item['category']}): 수집 {item['collected']}건",
            f"  마지막 로컬 기록: {item['last_collected_at'] or '없음'}",
            f"  마지막 이력 확인: {history.get('last_checked_at') or '아직 확인 전'} / 연속 실패 {history.get('failures', 0)}회",
        ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not database.DB_PATH.is_file():
        parser.error("수집 DB가 없습니다. 봇을 먼저 실행하세요.")
    channel_map = json.loads((database.BASE_DIR / "config/channels.json").read_text(encoding="utf-8"))["channels"]
    snapshot = database.health_snapshot(channel_map)
    print(json.dumps(snapshot, ensure_ascii=False, indent=2) if args.json else format_status(snapshot))


if __name__ == "__main__":
    main()
