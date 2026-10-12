<template>
    <div v-if="windowTouch">
        <Button :loading="loading" type="primary" icon="ios-search" @click="onSearch">{{$L('搜索')}}</Button>
        <Button v-if="filtering" type="text" @click="onCancelFilter">{{$L('取消筛选')}}</Button>
        <Button v-else :loading="loading" type="text" icon="md-refresh" @click="onRefresh">{{$L('刷新')}}</Button>
    </div>
    <Tooltip
        v-else
        theme="light"
        :placement="placement"
        transfer-class-name="search-button-clear"
        transfer>
        <Button :loading="loading" type="primary" icon="ios-search" @click="onSearch">{{$L('搜索')}}</Button>
        <div slot="content">
            <Button v-if="filtering" type="text" @click="onCancelFilter">{{$L('取消筛选')}}</Button>
            <Button v-else :loading="loading" type="text" @click="onRefresh">{{$L('刷新')}}</Button>
        </div>
    </Tooltip>
</template>

<script>

export default {
    name: 'SearchButton',
    props: {
        loading: {
            type: Boolean,
            default: false
        },
        filtering: {
            type: Boolean,
            default: false
        },
        placement: {
            type: String,
            default: 'bottom'
        }
    },

    methods: {
        onSearch() {
            this.$emit('search');
        },
        onRefresh() {
            this.$emit('refresh');
        },
        onCancelFilter() {
            this.$emit('cancelFilter');
        }
    }
};
</script>
