import os
import json
import hashlib
import logging
from pathlib import Path

import discord
from dotenv import load_dotenv

from database import (
    init_db,
    insert_file,
    find_existing_file,
    update_drive_info
)

from drive import (
    get_drive_service,
    upload_file
)


load_dotenv()

TOKEN = os.getenv(
    "DISCORD_BOT_TOKEN"
)

BASE_DIR = Path(
    __file__
).resolve().parent.parent

STORAGE_DIR = (
    BASE_DIR
    / "storage"
)

LOG_DIR = (
    BASE_DIR
    / "logs"
)

CONFIG_PATH = (
    BASE_DIR
    / "config"
    / "channels.json"
)


LOG_DIR.mkdir(
    parents=True,
    exist_ok=True
)


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(message)s"
    ),
    handlers=[
        logging.FileHandler(
            LOG_DIR
            / "project-hub.log",
            encoding="utf-8"
        ),
        logging.StreamHandler()
    ]
)


logger = logging.getLogger(
    "project-hub"
)


def load_channel_map():
    with open(
        CONFIG_PATH,
        "r",
        encoding="utf-8"
    ) as f:

        config = json.load(
            f
        )

    return config[
        "channels"
    ]


def calculate_sha256(
    file_path
):
    sha256 = hashlib.sha256()

    with open(
        file_path,
        "rb"
    ) as f:

        for chunk in iter(
            lambda: f.read(
                1024 * 1024
            ),
            b""
        ):
            sha256.update(
                chunk
            )

    return sha256.hexdigest()


CHANNEL_MAP = load_channel_map()


intents = (
    discord.Intents.default()
)

intents.message_content = True


client = discord.Client(
    intents=intents
)


# DB 초기화
init_db()


# Google Drive 연결
drive_service = (
    get_drive_service()
)


@client.event
async def on_ready():

    logger.info(
        "Discord 로그인 완료 | "
        "bot=%s | "
        "감지채널=%d",
        client.user,
        len(
            CHANNEL_MAP
        )
    )


@client.event
async def on_message(
    message
):

    if message.author.bot:
        return

    if not message.attachments:
        return

    channel_id = str(
        message.channel.id
    )

    if (
        channel_id
        not in CHANNEL_MAP
    ):
        return

    category = (
        CHANNEL_MAP[
            channel_id
        ]
    )

    save_dir = (
        STORAGE_DIR
        / category
    )

    metadata_dir = (
        save_dir
        / "_metadata"
    )

    save_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    metadata_dir.mkdir(
        parents=True,
        exist_ok=True
    )


    for index, attachment in enumerate(
        message.attachments,
        start=1
    ):

        try:

            timestamp = (
                message
                .created_at
                .strftime(
                    "%Y%m%d_%H%M%S"
                )
            )

            original_filename = (
                attachment.filename
            )

            saved_filename = (
                f"{timestamp}_"
                f"{message.id}_"
                f"{index}_"
                f"{original_filename}"
            )

            save_path = (
                save_dir
                / saved_filename
            )


            # Discord → 로컬 저장
            await attachment.save(
                save_path
            )


            # 파일 크기
            file_size = (
                save_path
                .stat()
                .st_size
            )


            # SHA-256
            sha256 = (
                calculate_sha256(
                    save_path
                )
            )


            # 중복 / 버전 판정
            duplicate_info = (
                find_existing_file(
                    original_filename,
                    sha256
                )
            )


            # Discord 원문 URL
            if message.guild:

                discord_url = (
                    "https://discord.com/"
                    "channels/"
                    f"{message.guild.id}/"
                    f"{message.channel.id}/"
                    f"{message.id}"
                )

            else:

                discord_url = None


            # 메타데이터 구성
            metadata = {

                "discord_message_id":
                    str(
                        message.id
                    ),

                "discord_guild_id":
                    (
                        str(
                            message.guild.id
                        )
                        if message.guild
                        else None
                    ),

                "discord_channel_id":
                    str(
                        message.channel.id
                    ),

                "discord_channel":
                    message.channel.name,

                "discord_author_id":
                    str(
                        message.author.id
                    ),

                "discord_author":
                    str(
                        message.author
                    ),

                "uploaded_at":
                    (
                        message
                        .created_at
                        .isoformat()
                    ),

                "category":
                    category,

                "original_filename":
                    original_filename,

                "saved_filename":
                    saved_filename,

                "local_path":
                    str(
                        save_path
                    ),

                "file_size_bytes":
                    file_size,

                "sha256":
                    sha256,

                "duplicate_of":
                    duplicate_info[
                        "duplicate_of"
                    ],

                "version_group":
                    duplicate_info[
                        "version_group"
                    ],

                "duplicate_type":
                    duplicate_info[
                        "type"
                    ],

                "discord_url":
                    discord_url
            }


            # SQLite INSERT
            db_file_id = (
                insert_file(
                    metadata
                )
            )


            # Google Drive 업로드
            drive_result = (
                upload_file(
                    drive_service,
                    save_path,
                    category,
                    original_filename
                )
            )


            # Drive 정보 DB 업데이트
            update_drive_info(
                db_file_id,
                drive_result[
                    "file_id"
                ],
                drive_result[
                    "url"
                ]
            )


            # JSON 메타데이터에도
            # Drive 결과 추가
            metadata[
                "google_drive_file_id"
            ] = drive_result[
                "file_id"
            ]

            metadata[
                "google_drive_url"
            ] = drive_result[
                "url"
            ]


            metadata_filename = (
                f"{timestamp}_"
                f"{message.id}_"
                f"{index}.json"
            )


            metadata_path = (
                metadata_dir
                / metadata_filename
            )


            with open(
                metadata_path,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    metadata,
                    f,
                    ensure_ascii=False,
                    indent=2
                )


            logger.info(
                "FILE_SAVED | "
                "category=%s | "
                "channel=%s | "
                "author=%s | "
                "file=%s | "
                "size=%d | "
                "sha256=%s | "
                "duplicate_type=%s | "
                "duplicate_of=%s | "
                "drive_file_id=%s | "
                "message_id=%s",

                category,
                message.channel.name,
                message.author,
                original_filename,
                file_size,
                sha256,
                duplicate_info[
                    "type"
                ],
                duplicate_info[
                    "duplicate_of"
                ],
                drive_result[
                    "file_id"
                ],
                message.id
            )


        except Exception:

            logger.exception(
                "FILE_PROCESS_FAILED | "
                "channel=%s | "
                "message_id=%s | "
                "file=%s",

                message.channel.name,
                message.id,
                attachment.filename
            )


if not TOKEN:

    raise RuntimeError(
        "DISCORD_BOT_TOKEN이 "
        ".env 파일에 없습니다."
    )


client.run(
    TOKEN
)