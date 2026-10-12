<template>
    <Modal
        :value="show"
        :width="416"
        :closable="false"
        :mask-closable="false"
        :footer-hide="true">
        <div class="ivu-modal-confirm">
            <div class="ivu-modal-confirm-head">
                <div class="ivu-modal-confirm-head-icon ivu-modal-confirm-head-icon-error"><Icon type="ios-close-circle"/></div><div class="ivu-modal-confirm-head-title">{{$L('温馨提示')}}</div>
            </div>
            <div class="ivu-modal-confirm-body">
                <div>{{ajaxAuthException}}</div>
            </div>
            <div v-if="$isSubElectron" class="ivu-modal-confirm-footer">
                <Button type="text" @click="onClose">{{$L('关闭窗口')}}</Button>
                <Button type="primary" @click="onRefresh">{{$L('刷新')}}</Button>
            </div>
            <div v-else class="ivu-modal-confirm-footer">
                <Button type="primary" @click="onConfirm">{{$L('确定')}}</Button>
            </div>
        </div>
    </Modal>
</template>

<script>
import {mapState} from "vuex";

export default {
    name: 'AuthException',
    computed: {
        ...mapState(['ajaxAuthException']),

        show() {
            return this.routePath !== '/login' && !!this.ajaxAuthException
        }
    },

    methods: {
        onClose() {
            window.close();
        },

        onRefresh() {
            $A.reloadUrl()
        },

        onConfirm() {
            this.$store.state.ajaxAuthException = null
            this.$store.dispatch("logout")
        }
    }
}
</script>
