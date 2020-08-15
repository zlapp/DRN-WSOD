from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from detectron.core.config import cfg
from detectron.utils.net import get_group_gn

from detectron.modeling.ResNet import basic_bn_shortcut
from detectron.modeling.ResNet import basic_gn_shortcut

# ---------------------------------------------------------------------------- #
# Bits for specific architectures (ResNet50, ResNet101, ...)
# ---------------------------------------------------------------------------- #


def add_ResNet18_conv4_body(model):
    return add_shallow_ResNet_convX_body(model, (2, 2, 2))


def add_ResNet18_conv5_body(model):
    return add_shallow_ResNet_convX_body(model, (2, 2, 2, 2))


def add_ResNet50_conv4_body(model):
    return add_ResNet_convX_body(model, (3, 4, 6))


def add_ResNet50_conv5_body(model):
    return add_ResNet_convX_body(model, (3, 4, 6, 3))


def add_ResNet101_conv4_body(model):
    return add_ResNet_convX_body(model, (3, 4, 23))


def add_ResNet101_conv5_body(model):
    return add_ResNet_convX_body(model, (3, 4, 23, 3))


def add_ResNet152_conv5_body(model):
    return add_ResNet_convX_body(model, (3, 8, 36, 3))


# ---------------------------------------------------------------------------- #
# Generic ResNet components
# ---------------------------------------------------------------------------- #


def add_stage(
    model,
    prefix,
    blob_in,
    n,
    dim_in,
    dim_out,
    dim_inner,
    dilation,
    stride_init=1,
    has_pool=False,
    stride_pool=2
):
    """Add a ResNet stage to the model by stacking n residual blocks."""
    # e.g., prefix = res2
    for i in range(n):
        blob_in = add_residual_block(
            model,
            '{}_{}'.format(prefix, i),
            blob_in,
            dim_in,
            dim_out,
            dim_inner,
            dilation,
            stride_init,
            # Not using inplace for the last block;
            # it may be fetched externally or used by FPN
            inplace_sum=i < n - 1
        )
        dim_in = dim_out

    if has_pool:
        blob_in = model.MaxPool(blob_in, blob_in + '_pool', kernel=2, pad=0, stride=stride_pool)

    return blob_in, dim_in


def add_shallow_ResNet_convX_body(model, block_counts):
    """Add a ResNet body from input data up through the res5 (aka conv5) stage.
    The final res5/conv5 stage may be optionally excluded (hence convX, where
    X = 4 or 5)."""
    freeze_at = cfg.TRAIN.FREEZE_AT
    assert freeze_at in [0, 2, 3, 4, 5]

    # add the stem (by default, conv1 and pool1 with bn; can support gn)
    p, dim_in = globals()[cfg.RESNETS.STEM_FUNC](model, 'data')

    (n1, n2, n3) = block_counts[:3]
    s, dim_in = add_stage(model, 'res2', p, n1, dim_in, 64, 0, 1, has_pool=True)
    if freeze_at == 2:
        model.StopGradient(s, s)
    s, dim_in = add_stage(
        model, 'res3', s, n2, dim_in, 128, 0, 1, has_pool=True, stride_pool=2 if cfg.RESNETS.RES5_DILATION == 1 else 1
    )
    if freeze_at == 3:
        model.StopGradient(s, s)
    s, dim_in = add_stage(
        model, 'res4', s, n3, dim_in, 256, 0, cfg.RESNETS.RES5_DILATION
    )
    if freeze_at == 4:
        model.StopGradient(s, s)
    if len(block_counts) == 4:
        n4 = block_counts[3]
        s, dim_in = add_stage(
            model, 'res5', s, n4, dim_in, 512, 0,
            cfg.RESNETS.RES5_DILATION
        )
        if freeze_at == 5:
            model.StopGradient(s, s)
        return s, dim_in, 1. / 16. * cfg.RESNETS.RES5_DILATION
    else:
        return s, dim_in, 1. / 8.


def add_ResNet_convX_body(model, block_counts):
    """Add a ResNet body from input data up through the res5 (aka conv5) stage.
    The final res5/conv5 stage may be optionally excluded (hence convX, where
    X = 4 or 5)."""
    freeze_at = cfg.TRAIN.FREEZE_AT
    assert freeze_at in [0, 2, 3, 4, 5]

    # add the stem (by default, conv1 and pool1 with bn; can support gn)
    p, dim_in = globals()[cfg.RESNETS.STEM_FUNC](model, 'data')

    dim_bottleneck = cfg.RESNETS.NUM_GROUPS * cfg.RESNETS.WIDTH_PER_GROUP
    (n1, n2, n3) = block_counts[:3]
    s, dim_in = add_stage(model, 'res2', p, n1, dim_in, 256, dim_bottleneck, 1, has_pool=True)
    if freeze_at == 2:
        model.StopGradient(s, s)
    s, dim_in = add_stage(
        model, 'res3', s, n2, dim_in, 512, dim_bottleneck * 2, 1, has_pool=True, stride_pool=2 if cfg.RESNETS.RES5_DILATION == 1 else 1
    )
    if freeze_at == 3:
        model.StopGradient(s, s)
    s, dim_in = add_stage(
        model, 'res4', s, n3, dim_in, 1024, dim_bottleneck * 4, cfg.RESNETS.RES5_DILATION
    )
    if freeze_at == 4:
        model.StopGradient(s, s)
    if len(block_counts) == 4:
        n4 = block_counts[3]
        s, dim_in = add_stage(
            model, 'res5', s, n4, dim_in, 2048, dim_bottleneck * 8,
            cfg.RESNETS.RES5_DILATION
        )
        if freeze_at == 5:
            model.StopGradient(s, s)
        return s, dim_in, 1. / 16. * cfg.RESNETS.RES5_DILATION
    else:
        return s, dim_in, 1. / 8.


def add_shallow_ResNet_roi_conv5_head(model, blob_in, dim_in, spatial_scale):
    """Adds an RoI feature transformation (e.g., RoI pooling) followed by a
    res5/conv5 head applied to each RoI."""
    # TODO(rbg): This contains Fast R-CNN specific config options making it non-
    # reusable; make this more generic with model-specific wrappers
    model.RoIFeatureTransform(
        blob_in,
        'pool5',
        blob_rois='rois',
        method=cfg.FAST_RCNN.ROI_XFORM_METHOD,
        resolution=cfg.FAST_RCNN.ROI_XFORM_RESOLUTION,
        sampling_ratio=cfg.FAST_RCNN.ROI_XFORM_SAMPLING_RATIO,
        spatial_scale=spatial_scale,
    )

    stride_init = int(cfg.FAST_RCNN.ROI_XFORM_RESOLUTION / 7)
    s, dim_in = add_stage(
        model, 'res5', 'pool5', 2, dim_in, 512, 0, 1,
        stride_init,
    )
    s = model.AveragePool(s, 'res5_pool', kernel=7)
    return s, 512


def add_ResNet_roi_conv5_head(model, blob_in, dim_in, spatial_scale):
    """Adds an RoI feature transformation (e.g., RoI pooling) followed by a
    res5/conv5 head applied to each RoI."""
    # TODO(rbg): This contains Fast R-CNN specific config options making it non-
    # reusable; make this more generic with model-specific wrappers
    model.RoIFeatureTransform(
        blob_in,
        'pool5',
        blob_rois='rois',
        method=cfg.FAST_RCNN.ROI_XFORM_METHOD,
        resolution=cfg.FAST_RCNN.ROI_XFORM_RESOLUTION,
        sampling_ratio=cfg.FAST_RCNN.ROI_XFORM_SAMPLING_RATIO,
        spatial_scale=spatial_scale
    )
    dim_bottleneck = cfg.RESNETS.NUM_GROUPS * cfg.RESNETS.WIDTH_PER_GROUP
    stride_init = int(cfg.FAST_RCNN.ROI_XFORM_RESOLUTION / 7)
    s, dim_in = add_stage(
        model, 'res5', 'pool5', 3, dim_in, 2048, dim_bottleneck * 8, 1,
        stride_init
    )
    s = model.AveragePool(s, 'res5_pool', kernel=7)
    return s, 2048


def add_ResNet_roi_2fc_head(model, blob_in, dim_in, spatial_scale):
    """Adds an RoI feature transformation (e.g., RoI pooling) followed by a
    res5/conv5 head applied to each RoI."""
    # TODO(rbg): This contains Fast R-CNN specific config options making it non-
    # reusable; make this more generic with model-specific wrappers
    model.RoIFeatureTransform(
        blob_in,
        'pool5',
        blob_rois='rois',
        method=cfg.FAST_RCNN.ROI_XFORM_METHOD,
        resolution=cfg.FAST_RCNN.ROI_XFORM_RESOLUTION,
        sampling_ratio=cfg.FAST_RCNN.ROI_XFORM_SAMPLING_RATIO,
        spatial_scale=spatial_scale,
    )

    if len(cfg.WSL.MLP_HEAD_DIM) == 2:
        dim_fc6 = cfg.WSL.MLP_HEAD_DIM[0]
        dim_fc7 = cfg.WSL.MLP_HEAD_DIM[1]
    else:
        dim_fc6 = cfg.FAST_RCNN.MLP_HEAD_DIM
        dim_fc7 = cfg.FAST_RCNN.MLP_HEAD_DIM

    fc6 = model.FC(
        'pool5',
        'fc6',
        dim_in * 7 * 7,
        dim_fc6,
    )
    relu6 = model.Relu(fc6, 'fc6')
    drop6 = model.Dropout(relu6, 'drop6', ratio=0.5, is_test=not model.train)

    fc7 = model.FC(
        drop6,
        'fc7',
        dim_fc6,
        dim_fc7,
    )
    relu7 = model.Relu(fc7, 'fc7')
    drop7 = model.Dropout(relu7, 'drop7', ratio=0.5, is_test=not model.train)
    return drop7, dim_fc7


def add_residual_block(
    model,
    prefix,
    blob_in,
    dim_in,
    dim_out,
    dim_inner,
    dilation,
    stride_init=2,
    inplace_sum=False
):
    """Add a residual block to the model."""
    # prefix = res<stage>_<sub_stage>, e.g., res2_3

    # Max pooling is performed prior to the first stage (which is uniquely
    # distinguished by dim_in = 64), thus we keep stride = 1 for the first stage
    stride = stride_init if (
        dim_in != dim_out and 'res2' not in prefix and dilation == 1
    ) else 1

    # transformation blob
    tr = globals()[cfg.RESNETS.TRANS_FUNC](
        model,
        blob_in,
        dim_in,
        dim_out,
        stride,
        prefix,
        dim_inner,
        group=cfg.RESNETS.NUM_GROUPS,
        dilation=dilation
    )

    # sum -> ReLU
    # shortcut function: by default using bn; support gn
    add_shortcut = globals()[cfg.RESNETS.SHORTCUT_FUNC]
    sc = add_shortcut(model, prefix, blob_in, dim_in, dim_out, stride)
    if inplace_sum:
        s = model.net.Sum([tr, sc], tr)
    else:
        s = model.net.Sum([tr, sc], prefix + '_sum')

    return model.Relu(s, s)


# ------------------------------------------------------------------------------
# various shortcuts (may expand and may consider a new helper)
# ------------------------------------------------------------------------------


# ------------------------------------------------------------------------------
# various stems (may expand and may consider a new helper)
# ------------------------------------------------------------------------------


def basic_bn_stem(model, data, **kwargs):
    """Add a basic ResNet stem. For a pre-trained network that used BN.
    An AffineChannel op replaces BN during fine-tuning.
    """

    # weight_init = None
    # weight_init = ('XavierFill', {})
    weight_init = ("MSRAFill", {})

    dim = 64
    p = model.Conv('data', 'conv1_1', 3, dim, 3, pad=1, stride=2, no_bias=1, weight_init=weight_init)
    p = model.AffineChannel(p, 'conv1_1_bn', dim=dim, inplace=True)
    p = model.Relu(p, p)

    p = model.Conv( p, 'conv1_2', dim, dim, 3, pad=1, stride=1, no_bias=1, weight_init=weight_init)
    p = model.AffineChannel(p, 'conv1_2_bn', dim=dim, inplace=True)
    p = model.Relu(p, p)

    p = model.Conv( p, 'conv1_3', dim, dim, 3, pad=1, stride=1, no_bias=1, weight_init=weight_init)
    p = model.AffineChannel(p, 'conv1_3_bn', dim=dim, inplace=True)
    p = model.Relu(p, p)
    p = model.MaxPool(p, 'pool1_3', kernel=2, pad=0, stride=2)
    return p, dim

    dim = 64
    p = model.Conv('data', 'conv1_1', 3, dim, 3, pad=1, stride=1, no_bias=1, weight_init=weight_init)
    p = model.AffineChannel(p, 'conv1_1_bn', dim=dim, inplace=True)
    p = model.Relu(p, p)
    p = model.MaxPool(p, 'pool1_1', kernel=2, pad=0, stride=2)
    p = model.Conv( p, 'conv1_2', dim, dim, 3, pad=1, stride=1, no_bias=1, weight_init=weight_init)
    p = model.AffineChannel(p, 'conv1_2_bn', dim=dim, inplace=True)
    p = model.Relu(p, p)
    p = model.MaxPool(p, 'pool1_2', kernel=2, pad=0, stride=2)
    return p, dim

    weight_init = ("MSRAFill", {})
    dim = 64
    p = model.Conv(data, 'conv1', 3, dim, 7, pad=3, stride=2, no_bias=1, weight_init=weight_init)
    p = model.AffineChannel(p, 'res_conv1_bn', dim=dim, inplace=True)
    p = model.Relu(p, p)
    p = model.MaxPool(p, 'pool1', kernel=3, pad=1, stride=2)
    return p, dim


def basic_gn_stem(model, data, **kwargs):
    """Add a basic ResNet stem (using GN)"""

    dim = 64
    p = model.ConvGN(
        data, 'conv1', 3, dim, 7, group_gn=get_group_gn(dim), pad=3, stride=2
    )
    p = model.Relu(p, p)
    p = model.MaxPool(p, 'pool1', kernel=3, pad=1, stride=2)
    return p, dim


# ------------------------------------------------------------------------------
# various transformations (may expand and may consider a new helper)
# ------------------------------------------------------------------------------


def residual_transformation(
        model,
        blob_in,
        dim_in,
        dim_out,
        stride,
        prefix,
        dim_inner,
        dilation=1,
        group=1,
):
    """Add a bottleneck transformation to the model."""

    # weight_init = None
    # weight_init = ('XavierFill', {})
    weight_init = ("MSRAFill", {})

    # conv 3x3 -> BN -> ReLU
    cur = model.ConvAffine(
        blob_in,
        prefix + '_branch2a',
        dim_in,
        dim_out,
        kernel=3,
        stride=stride,
        pad=1 * dilation,
        dilation=dilation,
        group=group,
        inplace=True,
        weight_init=weight_init,
    )
    cur = model.Relu(cur, cur)

    # conv 3x3 -> BN -> ReLU
    cur = model.ConvAffine(
        cur,
        prefix + '_branch2b',
        dim_out,
        dim_out,
        kernel=3,
        stride=1,
        pad=1 * dilation,
        dilation=dilation,
        group=group,
        inplace=False,
        weight_init=weight_init,
    )

    return cur


def bottleneck_transformation(
    model,
    blob_in,
    dim_in,
    dim_out,
    stride,
    prefix,
    dim_inner,
    dilation=1,
    group=1
):
    """Add a bottleneck transformation to the model."""
    # In original resnet, stride=2 is on 1x1.
    # In fb.torch resnet, stride=2 is on 3x3.
    (str1x1, str3x3) = (stride, 1) if cfg.RESNETS.STRIDE_1X1 else (1, stride)

    # weight_init = None
    # weight_init = ('XavierFill', {})
    weight_init = ("MSRAFill", {})

    # conv 1x1 -> BN -> ReLU
    cur = model.ConvAffine(
        blob_in,
        prefix + '_branch2a',
        dim_in,
        dim_inner,
        kernel=1,
        stride=str1x1,
        pad=0,
        inplace=True,
        weight_init=weight_init
    )
    cur = model.Relu(cur, cur)

    # conv 3x3 -> BN -> ReLU
    cur = model.ConvAffine(
        cur,
        prefix + '_branch2b',
        dim_inner,
        dim_inner,
        kernel=3,
        stride=str3x3,
        pad=1 * dilation,
        dilation=dilation,
        group=group,
        inplace=True,
        weight_init=weight_init
    )
    cur = model.Relu(cur, cur)

    # conv 1x1 -> BN (no ReLU)
    # NB: for now this AffineChannel op cannot be in-place due to a bug in C2
    # gradient computation for graphs like this
    cur = model.ConvAffine(
        cur,
        prefix + '_branch2c',
        dim_inner,
        dim_out,
        kernel=1,
        stride=1,
        pad=0,
        inplace=False,
        weight_init=weight_init
    )
    return cur


def bottleneck_gn_transformation(
    model,
    blob_in,
    dim_in,
    dim_out,
    stride,
    prefix,
    dim_inner,
    dilation=1,
    group=1
):
    """Add a bottleneck transformation with GroupNorm to the model."""
    # In original resnet, stride=2 is on 1x1.
    # In fb.torch resnet, stride=2 is on 3x3.
    (str1x1, str3x3) = (stride, 1) if cfg.RESNETS.STRIDE_1X1 else (1, stride)

    # conv 1x1 -> GN -> ReLU
    cur = model.ConvGN(
        blob_in,
        prefix + '_branch2a',
        dim_in,
        dim_inner,
        kernel=1,
        group_gn=get_group_gn(dim_inner),
        stride=str1x1,
        pad=0,
    )
    cur = model.Relu(cur, cur)

    # conv 3x3 -> GN -> ReLU
    cur = model.ConvGN(
        cur,
        prefix + '_branch2b',
        dim_inner,
        dim_inner,
        kernel=3,
        group_gn=get_group_gn(dim_inner),
        stride=str3x3,
        pad=1 * dilation,
        dilation=dilation,
        group=group,
    )
    cur = model.Relu(cur, cur)

    # conv 1x1 -> GN (no ReLU)
    cur = model.ConvGN(
        cur,
        prefix + '_branch2c',
        dim_inner,
        dim_out,
        kernel=1,
        group_gn=get_group_gn(dim_out),
        stride=1,
        pad=0,
    )
    return cur


def add_Resnet_2fc_head(model, blob_in, dim_in, spatial_scale, dim_fc6=4096, dim_fc7=4096):
    pool5 = model.MaxPool(blob_in, 'pool5', kernel=2, pad=0, stride=2)

    # weight_init = None
    weight_init = ('GaussianFill', {'std': 0.005})
    # weight_init = ('XavierFill', {})
    # weight_init = ("MSRAFill", {})

    bias_init = ('ConstantFill', {'value': 0.1})

    fc6 = model.FC(
        pool5,
        'fc6',
        dim_in * 7 * 7,
        dim_fc6,
        weight_init=weight_init,
        bias_init=bias_init,
    )
    relu6 = model.Relu(fc6, 'fc6')
    drop6 = model.Dropout(relu6, 'drop6', ratio=0.5, is_test=not model.train)

    fc7 = model.FC(
        drop6,
        'fc7',
        dim_fc6,
        dim_fc7,
        weight_init=weight_init,
        bias_init=bias_init,
    )
    relu7 = model.Relu(fc7, 'fc7')
    drop7 = model.Dropout(relu7, 'drop7', ratio=0.5, is_test=not model.train)
    return drop7, dim_fc7, spatial_scale / 2.