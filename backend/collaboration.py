from sqlalchemy import select
from . import db
from .context import utcnow
from .errors import ApiError


def create_personal_project(connection, userid):
    """Registration/first login only; atomic owner, column, project chat and log.

    This is not the general project API: no workflow/other members or external
    delivery is implied. Caller holds the user's row lock until commit.
    """
    users = db.tables["users"]
    user = connection.execute(select(users.c.userid).where(users.c.userid == userid).with_for_update()).first()
    if user is None:
        raise ApiError("会员不存在")
    projects = db.tables["projects"]
    existing = connection.execute(select(projects.c.id).where(projects.c.userid == userid, projects.c.personal == 1)).first()
    # Source login deliberately includes soft-deleted personal projects.
    if existing:
        return existing.id
    name = "\U0001f4dd 个人项目"
    project_id = db.insert_row(connection, "projects", name=name,
                              desc="注册时系统自动创建项目，你可以自由删除。", userid=userid, personal=1)
    db.insert_row(connection, "project_users", project_id=project_id, userid=userid, owner=1)
    db.insert_row(connection, "project_columns", project_id=project_id, name="Default", sort=0)
    dialog_id = db.insert_row(connection, "web_socket_dialogs", type="group", group_type="project", name=name, owner_id=userid)
    db.insert_row(connection, "web_socket_dialog_users", dialog_id=dialog_id, userid=userid,
                  role=1, bot=0, important=1, last_at=None)
    connection.execute(projects.update().where(projects.c.id == project_id).values(dialog_id=dialog_id, updated_at=utcnow()))
    db.insert_row(connection, "project_logs", project_id=project_id, column_id=0, task_id=0,
                  userid=userid, detail="创建项目")
    return project_id
