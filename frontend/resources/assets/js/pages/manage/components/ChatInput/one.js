import Vue from 'vue';
import Emoji from "./emoji.vue";
import {Modal} from "view-design-hi";

// 加载中的输入框的uid，主要用于判断是否最后一个输入框
const inputLoadUid = []

function inputLoadIsLast(uid) {
    if (inputLoadUid.length === 0) {
        return true
    }
    const index = inputLoadUid.indexOf(uid)
    if (index === -1) {
        return false
    }
    return index === inputLoadUid.length - 1
}

const inputLoadRemove = (uid) => {
    const index = inputLoadUid.indexOf(uid)
    if (index !== -1) {
        inputLoadUid.splice(index, 1)
    }
}

const inputLoadAdd = (uid) => {
    inputLoadRemove(uid)
    inputLoadUid.push(uid)
}

function choiceEmojiOne() {
    return new Promise(resolve => {
        const Instance = new Vue({
            render (h) {
                return h(Modal, {
                    class: 'chat-emoji-one-modal',
                    props: {
                        fullscreen: true,
                        footerHide: true,
                    },
                    on: {
                        'on-visible-change': (v) => {
                            if (!v) {
                                setTimeout(_ => {
                                    document.body.removeChild(this.$el);
                                }, 500)
                            }
                        }
                    }
                }, [
                    h(Emoji, {
                        attrs: {
                            onlyEmoji: true
                        },
                        on: {
                            'on-select': (item) => {
                                this.$children[0].visible = false
                                if (item.type === 'emoji') {
                                    resolve(item.text)
                                }
                            },
                        }
                    })
                ]);
            }
        });

        const component = Instance.$mount();
        document.body.appendChild(component.$el);

        const modal = Instance.$children[0];
        modal.visible = true;

        modal.$el.lastChild.addEventListener("click", ({target}) => {
            if (target.classList.contains("ivu-modal-body")) {
                modal.visible = false
            }
        });
    })
}

export {choiceEmojiOne, inputLoadAdd, inputLoadRemove, inputLoadIsLast}
