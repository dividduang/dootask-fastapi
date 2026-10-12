<template>
    <div class="setting-item submit">
        <div class="version-box">
            <div v-if="loadIng" class="version-load">{{$L('加载中...')}}</div>
            <VMPreview v-else :value="updateLog"/>
        </div>
    </div>
</template>

<style lang="scss">
.version-box {
    overflow: auto;
    padding: 24px 34px;
    .version-load {
        font-size: 16px;
    }
    .vuepress-markdown-body {
        padding: 0 !important;
        color: inherit;
        h1, h2, h3, h4, h5, h6 {
            line-height: 1.25;
        }
        h2 {
            padding-bottom: .4em;
            font-size: 1.5em;
            margin: 1.5em 0 1em;
        }
        h3 {
            font-size: 1.2em;
            margin: 1.5em 0 1em;
        }
        ul, ol {
            padding-left: 2em;
            line-height: 1.5;
        }
        li {
            margin-top: .25em;
        }
    }
}
</style>
<script>
import VMPreview from "../../../components/VMEditor/preview.vue";

export default {
    components: {VMPreview},
    data() {
        return {
            loadIng: 0,
            updateLog: '',
        }
    },
    mounted() {
        this.getLog();
    },
    methods: {
        getLog() {
            this.loadIng++;
            this.$store.dispatch("call", {
                url: 'system/get/updatelog',
                data: {
                    take: 50
                }
            }).then(({data}) => {
                this.updateLog = data.updateLog;
            }).catch(({msg}) => {
                $A.messageError(msg);
            }).finally(_ => {
                this.loadIng--;
            });
        },
    }
}
</script>
