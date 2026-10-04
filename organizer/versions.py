"""Read-only upload relationships; a matching name is not proof of an approved revision."""

GROUP = 'x.original_filename=f.original_filename AND x.discord_channel_id IS f.discord_channel_id'
NEWER = '(COALESCE(julianday(x.uploaded_at),0)>COALESCE(julianday(f.uploaded_at),0) OR (COALESCE(julianday(x.uploaded_at),0)=COALESCE(julianday(f.uploaded_at),0) AND x.id>f.id))'
LATEST = 'NOT EXISTS (SELECT 1 FROM files x WHERE '+GROUP+' AND '+NEWER+')'


def info(db, file_id):
    return dict(db.execute('''SELECT
        (SELECT count(*) FROM files x WHERE '''+GROUP+''') AS upload_count,
        (SELECT count(DISTINCT x.sha256) FROM files x WHERE '''+GROUP+''') AS content_versions,
        (SELECT count(*) FROM files x WHERE x.id!=f.id AND x.sha256=f.sha256 AND length(f.sha256)>0) AS identical_count,
        (SELECT x.id FROM files x WHERE '''+GROUP+''' ORDER BY COALESCE(julianday(x.uploaded_at),0) DESC,x.id DESC LIMIT 1) AS latest_id
        FROM files f WHERE f.id=?''',(file_id,)).fetchone())


def related(db, file_id):
    rows=db.execute('''SELECT x.id,x.original_filename,x.discord_channel,x.uploaded_at,
        (x.sha256=f.sha256 AND length(f.sha256)>0) AS identical,
        ('''+GROUP+''') AS same_group
        FROM files f JOIN files x ON (('''+GROUP+''') OR (x.sha256=f.sha256 AND length(f.sha256)>0))
        WHERE f.id=? AND x.id!=f.id
        ORDER BY COALESCE(julianday(x.uploaded_at),0) DESC,x.id DESC LIMIT 51''',(file_id,)).fetchall()
    return [dict(r) for r in rows[:50]],len(rows)>50
