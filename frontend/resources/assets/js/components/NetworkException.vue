<template>
    <div class="common-network-exception">
        <template v-if="type==='alert'">
            <Alert v-if="show" type="error" show-icon closable @on-close="onClose">{{$L('网络连接失败，请检查网络设置。')}}</Alert>
        </template>
        <template v-else-if="type==='modal'">
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
                        <div>{{ajaxNetworkException}}</div>
                    </div>
                    <div class="ivu-modal-confirm-footer">
                        <Button type="text" @click="onClose">{{$L('忽略')}}</Button>
                        <Button type="primary" :loading="loadIng" @click="onCheck">{{$L('检查')}}</Button>
                    </div>
                </div>
            </Modal>
        </template>
    </div>
</template>

<script>
import {mapState} from "vuex";

export default {
    name: 'NetworkException',
    props: {
        type: {
            type: String,
            default: 'modal'
        },
    },
    data() {
        return {
            timer: null,
            checkIng: false,
            loadIng: false
        }
    },

    beforeDestroy() {
        this.onClose()
    },

    computed: {
        ...mapState(['ajaxNetworkException']),

        show() {
            return !!this.ajaxNetworkException
        }
    },

    watch: {
        show(v) {
            this.timer && clearInterval(this.timer)
            if (v) {
                this.timer = setInterval(this.checkNetwork, 3000)
            }
        }
    },

    methods: {
        isNotServer() {
            let apiHome = $A.getDomain(window.systemInfo.apiUrl)
            return this.$isSoftware && (apiHome == "" || apiHome == "public")
        },

        /**
         * 调用网络
         * @returns {Promise<void>}
         */
        async callNetwork() {
            if (this.isNotServer()) {
                this.onClose()
                return;
            }
            await this.$store.dispatch("call", {
                url: "system/setting",
            })
            this.onClose()
        },

        /**
         * 检查网络（自动）
         * @returns {Promise<void>}
         */
        async checkNetwork() {
            if (this.checkIng) {
                return
            }
            this.checkIng = true
            try {
                await this.callNetwork()
            } catch (e) {
                //
            }
            this.checkIng = false
        },

        /**
         * 检查网络（重试）
         * @returns {Promise<void>}
         */
        async onCheck() {
            if (this.loadIng) {
                return
            }
            this.loadIng = true
            try {
                await this.callNetwork()
            } catch (e) {
                $A.messageError("网络连接失败")
            }
            this.loadIng = false
        },

        /**
         * 关闭提示
         */
        onClose() {
            this.$store.state.ajaxNetworkException = null
        }
    }
}
</script>
