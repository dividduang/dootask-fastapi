<template>
    <Modal
        v-model="show"
        :title="$L('计划时间冲突提示')"
        :styles="{
            width: '90%',
            maxWidth: '600px'
        }"
        class="task-exist-tips">
        <List :split="false" size="small">
            <ListItem v-for="(items, userid) in tipsTask" :key="`a_${userid}`">
                <div class="list-content">
                    <UserAvatar class="list-task-avatar" :userid="userid" :size="28" :show-icon="true" :show-name="true"/>
                    <template v-for="data in formatItems(items)">
                        <div class="list-task label">
                            <div class="list-task-name" :title="data.project_name">{{ data.project_name }}</div>
                        </div>
                        <div class="list-task" v-for="item in data.list" :key="item.id">
                            <div class="list-task-name" :title="item.name">{{ item.name }}</div>
                            <div class="list-task-date">{{ getCutTime(item) }}</div>
                        </div>
                    </template>
                </div>
            </ListItem>
        </List>
        <div slot="footer">
            <Button type="default" @click="show = false">{{ $L('取消') }}</Button>
            <Button type="primary" @click="onContinue">{{ $L('忽略并继续') }}</Button>
        </div>
    </Modal>
</template>

<script>
export default {
    name: "TaskExistTips",
    props: {
        value: {
            type: Boolean,
            default: false
        },
    },
    data() {
        return {
            show: false,
            tipsTask: [],
        }
    },

    methods: {
        onContinue() {
            this.$emit('onContinue')
            this.show = false;
        },

        getCutTime(item) {
            let start_at = $A.dayjs(item.start_at);
            let end_at = $A.dayjs(item.end_at);
            let string = "";
            if (start_at.format('YY/MM/DD') == end_at.format('YY/MM/DD')) {
                string = start_at.format('YY/MM/DD HH:mm') + " ~ " + end_at.format('HH:mm')
            } else if (start_at.year() == end_at.year()) {
                string = start_at.format('YY/MM/DD HH:mm') + " ~ " + end_at.format('MM/DD HH:mm')
                string = string.replace(/( 00:00| 23:59)/g, "")
            } else {
                string = start_at.format('YY/MM/DD HH:mm') + " ~ " + end_at.format('YY/MM/DD HH:mm')
                string = string.replace(/( 00:00| 23:59)/g, "")
            }
            return string
        },

        isExistTask({userids, timerange, taskid}, spinner) {
            return new Promise(async resolve => {
                if ($A.isArray(timerange) && (!timerange[0] || !timerange[1])) {
                    resolve(false)
                    return
                }
                this.$store.dispatch("call", {
                    url: 'project/task/easylists',
                    data: {
                        userid: userids,
                        timerange: timerange,
                        taskid: taskid
                    },
                    method: 'get',
                    spinner,
                }).then(({data}) => {
                    if (data.data.length <= 0) {
                        resolve(false)
                        return
                    }
                    this.show = true;
                    const taskObj = {}
                    userids.map(userid => {
                        data.data.map(h => {
                            if ((h.task_user || []).map(k => k.owner ? k.userid : 0).indexOf(userid) !== -1) {
                                if (!taskObj[userid]) {
                                    taskObj[userid] = [];
                                }
                                taskObj[userid].push(h);
                            }
                        });
                    });
                    this.tipsTask = taskObj
                    resolve(true)
                });
            });
        },

        formatItems(items) {
            return Array.from(
                items.reduce((map, item) => {
                    if (!map.has(item.project_id)) {
                        map.set(item.project_id, {
                            project_id: item.project_id,
                            project_name: item.project_name,
                            list: []
                        });
                    }
                    map.get(item.project_id).list.push(item);
                    return map;
                }, new Map()).values()
            );
        }
    }
}
</script>
