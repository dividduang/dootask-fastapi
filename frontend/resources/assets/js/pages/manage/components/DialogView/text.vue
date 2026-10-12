<template>
    <div class="content-text no-dark-content">
        <div v-if="isOverdueMsg" class="content-overdue">{{$L('此消息已经过期')}}</div>
        <DialogMarkdown v-else-if="msg.type === 'md'" @click="viewText" :text="msg.text"/>
        <pre v-else @click="viewText" v-html="$A.formatTextMsg(msg.text, userId)"></pre>

        <template v-if="translation">
            <div class="content-divider">
                <span></span>
                <div class="divider-label translation-label" @click="viewText">{{ translation.label }}</div>
                <span></span>
            </div>
            <DialogMarkdown v-if="msg.type === 'md'" :text="translation.content" class="content-translation"/>
            <pre v-else v-html="$A.formatTextMsg(translation.content, userId)"></pre>
        </template>
    </div>
</template>

<script>
import {mapState} from "vuex";
import DialogMarkdown from "../DialogMarkdown.vue";

export default {
    components: {DialogMarkdown},
    props: {
        msgId: Number,
        msg: Object,
        createdAt: String,
    },
    computed: {
        ...mapState(['cacheTranslations', 'cacheTranslationLanguage']),

        translation({cacheTranslations, msgId, cacheTranslationLanguage}) {
            const translation = cacheTranslations.find(item => {
                return item.key === `msg-${msgId}` && item.language === cacheTranslationLanguage;
            });
            return translation ? translation : null;
        },

        isOverdueMsg({msg, createdAt}) {
            return msg.text === '...' && $A.dayjs(createdAt).isBefore($A.daytz().subtract(10, 'minute'));
        },
    },
    methods: {
        viewText(e) {
            this.$emit('viewText', e);
        },
    },
}
</script>
