import { BitmapLayer } from "@deck.gl/layers";
import type { BitmapLayerProps } from "@deck.gl/layers";
import type { UpdateParameters } from "@deck.gl/core";
import type { SamplerProps, Texture } from "@luma.gl/core";
import type { Model } from "@luma.gl/engine";
import type { ShaderModule } from "@luma.gl/shadertools";

const zarrRasterUniformBlock = `\
uniform zarrRasterUniforms {
  vec2 scalarRange;
} zarrRaster;
`;

const zarrRasterUniforms = {
  name: "zarrRaster",
  fs: zarrRasterUniformBlock,
  uniformTypes: {
    scalarRange: "vec2<f32>"
  }
} as const satisfies ShaderModule<
  ZarrColormapBitmapLayerProps,
  {
    scalarRange?: [number, number];
  }
>;

const zarrRasterFragmentShader = `\
#version 300 es
#define SHADER_NAME zarr-raster-fragment-shader

#ifdef GL_ES
precision highp float;
#endif

uniform sampler2D bitmapTexture;
uniform sampler2D paletteTexture;

in vec2 vTexCoord;
in vec2 vTexPos;

out vec4 fragColor;

const float TILE_SIZE = 512.0;
const float PI = 3.1415926536;
const float WORLD_SCALE = TILE_SIZE / PI / 2.0;

vec2 lnglat_to_mercator(vec2 lnglat) {
  float x = lnglat.x;
  float y = clamp(lnglat.y, -89.9, 89.9);
  return vec2(
    radians(x) + PI,
    PI + log(tan(PI * 0.25 + radians(y) * 0.5))
  ) * WORLD_SCALE;
}

vec2 mercator_to_lnglat(vec2 xy) {
  xy /= WORLD_SCALE;
  return degrees(vec2(
    xy.x - PI,
    atan(exp(xy.y - PI)) * 2.0 - PI * 0.5
  ));
}

vec4 apply_opacity(vec3 color, float alpha) {
  if (bitmap.transparentColor.a == 0.0) {
    return vec4(color, alpha);
  }
  float blendedAlpha = alpha + bitmap.transparentColor.a * (1.0 - alpha);
  float highLightRatio = alpha / blendedAlpha;
  vec3 blendedRGB = mix(bitmap.transparentColor.rgb, color, highLightRatio);
  return vec4(blendedRGB, blendedAlpha);
}

vec2 getUV(vec2 pos) {
  return vec2(
    (pos.x - bitmap.bounds[0]) / (bitmap.bounds[2] - bitmap.bounds[0]),
    (pos.y - bitmap.bounds[3]) / (bitmap.bounds[1] - bitmap.bounds[3])
  );
}

vec3 packUVsIntoRGB(vec2 uv) {
  vec2 uv8bit = floor(uv * 256.);
  vec2 uvFraction = fract(uv * 256.);
  vec2 uvFraction4bit = floor(uvFraction * 16.);
  float fractions = uvFraction4bit.x + uvFraction4bit.y * 16.;
  return vec3(uv8bit, fractions) / 255.;
}

void main(void) {
  vec2 uv = vTexCoord;
  if (bitmap.coordinateConversion < -0.5) {
    vec2 lnglat = mercator_to_lnglat(vTexPos);
    uv = getUV(lnglat);
  } else if (bitmap.coordinateConversion > 0.5) {
    vec2 commonPos = lnglat_to_mercator(vTexPos);
    uv = getUV(commonPos);
  }

  float rawValue = texture(bitmapTexture, uv).r;
  bool hasValue = !(isnan(rawValue) || isinf(rawValue));
  float denominator = zarrRaster.scalarRange.y > zarrRaster.scalarRange.x
    ? zarrRaster.scalarRange.y - zarrRaster.scalarRange.x
    : 1.0;
  float scalar = clamp((rawValue - zarrRaster.scalarRange.x) / denominator, 0.0, 1.0);
  vec4 paletteColor = texture(paletteTexture, vec2(scalar, 0.5));
  float alpha = hasValue ? paletteColor.a * layer.opacity : 0.0;
  fragColor = apply_opacity(paletteColor.rgb, alpha);

  geometry.uv = uv;
  DECKGL_FILTER_COLOR(fragColor, geometry);

  if (bool(picking.isActive) && !bool(picking.isAttribute)) {
    fragColor.rgb = packUVsIntoRGB(uv);
  }
}
`;

const zarrCompositeUniformBlock = `\
uniform zarrCompositeUniforms {
  vec2 redRange;
  vec2 greenRange;
  vec2 blueRange;
} zarrComposite;
`;

const zarrCompositeUniforms = {
  name: "zarrComposite",
  fs: zarrCompositeUniformBlock,
  uniformTypes: {
    redRange: "vec2<f32>",
    greenRange: "vec2<f32>",
    blueRange: "vec2<f32>"
  }
} as const satisfies ShaderModule<
  ZarrCompositeBitmapLayerProps,
  {
    redRange?: [number, number];
    greenRange?: [number, number];
    blueRange?: [number, number];
  }
>;

const zarrCompositeFragmentShader = `\
#version 300 es
#define SHADER_NAME zarr-composite-fragment-shader

#ifdef GL_ES
precision highp float;
#endif

uniform sampler2D bitmapTexture;
uniform sampler2D greenTexture;
uniform sampler2D blueTexture;

in vec2 vTexCoord;
in vec2 vTexPos;

out vec4 fragColor;

const float TILE_SIZE = 512.0;
const float PI = 3.1415926536;
const float WORLD_SCALE = TILE_SIZE / PI / 2.0;

vec2 lnglat_to_mercator(vec2 lnglat) {
  float x = lnglat.x;
  float y = clamp(lnglat.y, -89.9, 89.9);
  return vec2(
    radians(x) + PI,
    PI + log(tan(PI * 0.25 + radians(y) * 0.5))
  ) * WORLD_SCALE;
}

vec2 mercator_to_lnglat(vec2 xy) {
  xy /= WORLD_SCALE;
  return degrees(vec2(
    xy.x - PI,
    atan(exp(xy.y - PI)) * 2.0 - PI * 0.5
  ));
}

vec2 getUV(vec2 pos) {
  return vec2(
    (pos.x - bitmap.bounds[0]) / (bitmap.bounds[2] - bitmap.bounds[0]),
    (pos.y - bitmap.bounds[3]) / (bitmap.bounds[1] - bitmap.bounds[3])
  );
}

vec3 packUVsIntoRGB(vec2 uv) {
  vec2 uv8bit = floor(uv * 256.);
  vec2 uvFraction = fract(uv * 256.);
  vec2 uvFraction4bit = floor(uvFraction * 16.);
  float fractions = uvFraction4bit.x + uvFraction4bit.y * 16.;
  return vec3(uv8bit, fractions) / 255.;
}

float normalize_channel(float rawValue, vec2 range) {
  float denominator = range.y > range.x ? range.y - range.x : 1.0;
  return clamp((rawValue - range.x) / denominator, 0.0, 1.0);
}

void main(void) {
  vec2 uv = vTexCoord;
  if (bitmap.coordinateConversion < -0.5) {
    vec2 lnglat = mercator_to_lnglat(vTexPos);
    uv = getUV(lnglat);
  } else if (bitmap.coordinateConversion > 0.5) {
    vec2 commonPos = lnglat_to_mercator(vTexPos);
    uv = getUV(commonPos);
  }

  float redValue = texture(bitmapTexture, uv).r;
  float greenValue = texture(greenTexture, uv).r;
  float blueValue = texture(blueTexture, uv).r;
  bool hasValue = !(
    isnan(redValue) || isinf(redValue) ||
    isnan(greenValue) || isinf(greenValue) ||
    isnan(blueValue) || isinf(blueValue)
  );
  vec3 color = vec3(
    normalize_channel(redValue, zarrComposite.redRange),
    normalize_channel(greenValue, zarrComposite.greenRange),
    normalize_channel(blueValue, zarrComposite.blueRange)
  );
  fragColor = vec4(color, hasValue ? layer.opacity : 0.0);

  geometry.uv = uv;
  DECKGL_FILTER_COLOR(fragColor, geometry);

  if (bool(picking.isActive) && !bool(picking.isAttribute)) {
    fragColor.rgb = packUVsIntoRGB(uv);
  }
}
`;

export type ZarrColormapBitmapLayerProps = BitmapLayerProps & {
  rawValues: RawScalarTextureSource | null;
  paletteTexture: ImageData;
  scalarRange: [number, number];
};

export interface RawScalarTextureSource {
  data: Float32Array;
  width: number;
  height: number;
}

export type ZarrCompositeBitmapLayerProps = BitmapLayerProps & {
  redValues: RawScalarTextureSource | null;
  greenValues: RawScalarTextureSource | null;
  blueValues: RawScalarTextureSource | null;
  redRange: [number, number];
  greenRange: [number, number];
  blueRange: [number, number];
};

const defaultProps = {
  ...BitmapLayer.defaultProps,
  rawValues: { type: "object", value: null },
  paletteTexture: { type: "image", value: null, async: false },
  scalarRange: { type: "array", value: [0, 1], compare: true }
};

interface ZarrColormapBitmapLayerState {
  model?: Model;
  coordinateConversion: number;
  bounds: [number, number, number, number];
  disablePicking?: boolean;
  scalarTexture?: Texture | null;
}

export class ZarrColormapBitmapLayer extends BitmapLayer<ZarrColormapBitmapLayerProps> {
  static layerName = "ZarrColormapBitmapLayer";
  static defaultProps = defaultProps;

  getShaders() {
    const shaders = super.getShaders();
    return {
      ...shaders,
      fs: zarrRasterFragmentShader,
      modules: [...(shaders.modules ?? []), zarrRasterUniforms]
    };
  }

  updateState(params: UpdateParameters<this>) {
    super.updateState(params);
    const { props, oldProps } = params;
    if (
      props.rawValues !== oldProps.rawValues ||
      props.textureParameters !== oldProps.textureParameters
    ) {
      this.replaceScalarTexture(props.rawValues, props.textureParameters);
    }
  }

  finalizeState(context: Parameters<BitmapLayer<ZarrColormapBitmapLayerProps>["finalizeState"]>[0]) {
    this.destroyScalarTexture();
    super.finalizeState(context);
  }

  draw(opts: unknown) {
    const { shaderModuleProps } = opts as { shaderModuleProps: { picking: { isActive: boolean } } };
    const {
      model,
      coordinateConversion,
      bounds,
      disablePicking,
      scalarTexture
    } = this.state as unknown as ZarrColormapBitmapLayerState;
    const {
      paletteTexture,
      scalarRange,
      desaturate,
      transparentColor,
      tintColor
    } = this.props as unknown as ZarrColormapBitmapLayerProps & {
      desaturate: number;
      transparentColor: [number, number, number, number];
      tintColor: [number, number, number];
    };

    if (shaderModuleProps.picking.isActive && disablePicking) {
      return;
    }
    if (!scalarTexture || !paletteTexture || !model) {
      return;
    }

    model.shaderInputs.setProps({
      bitmap: {
        bitmapTexture: scalarTexture,
        bounds,
        coordinateConversion,
        desaturate,
        tintColor: tintColor.slice(0, 3).map((value) => value / 255),
        transparentColor: transparentColor.map((value) => value / 255)
      },
      zarrRaster: {
        paletteTexture,
        scalarRange
      }
    });
    model.draw(this.context.renderPass);
  }

  private replaceScalarTexture(
    rawValues: RawScalarTextureSource | null,
    textureParameters: SamplerProps | null | undefined
  ) {
    this.destroyScalarTexture();
    if (!rawValues) {
      this.setState({ scalarTexture: null });
      return;
    }

    const scalarTexture = this.context.device.createTexture({
      id: `${this.props.id}:raw-values`,
      data: rawValues.data,
      width: rawValues.width,
      height: rawValues.height,
      format: "r32float",
      mipLevels: 1,
      sampler: {
        addressModeU: "clamp-to-edge",
        addressModeV: "clamp-to-edge",
        minFilter: "nearest",
        magFilter: "nearest",
        mipmapFilter: "nearest",
        ...textureParameters
      }
    });
    this.setState({ scalarTexture });
  }

  private destroyScalarTexture() {
    const { scalarTexture } = this.state as unknown as ZarrColormapBitmapLayerState;
    scalarTexture?.destroy();
  }
}

const compositeDefaultProps = {
  ...BitmapLayer.defaultProps,
  redValues: { type: "object", value: null },
  greenValues: { type: "object", value: null },
  blueValues: { type: "object", value: null },
  redRange: { type: "array", value: [0, 1], compare: true },
  greenRange: { type: "array", value: [0, 1], compare: true },
  blueRange: { type: "array", value: [0, 1], compare: true }
};

interface ZarrCompositeBitmapLayerState extends ZarrColormapBitmapLayerState {
  redTexture?: Texture | null;
  greenTexture?: Texture | null;
  blueTexture?: Texture | null;
}

export class ZarrCompositeBitmapLayer extends BitmapLayer<ZarrCompositeBitmapLayerProps> {
  static layerName = "ZarrCompositeBitmapLayer";
  static defaultProps = compositeDefaultProps;

  getShaders() {
    const shaders = super.getShaders();
    return {
      ...shaders,
      fs: zarrCompositeFragmentShader,
      modules: [...(shaders.modules ?? []), zarrCompositeUniforms]
    };
  }

  updateState(params: UpdateParameters<this>) {
    super.updateState(params);
    const { props, oldProps } = params;
    if (
      props.redValues !== oldProps.redValues ||
      props.greenValues !== oldProps.greenValues ||
      props.blueValues !== oldProps.blueValues ||
      props.textureParameters !== oldProps.textureParameters
    ) {
      this.replaceCompositeTextures(props, props.textureParameters);
    }
  }

  finalizeState(context: Parameters<BitmapLayer<ZarrCompositeBitmapLayerProps>["finalizeState"]>[0]) {
    this.destroyCompositeTextures();
    super.finalizeState(context);
  }

  draw(opts: unknown) {
    const { shaderModuleProps } = opts as { shaderModuleProps: { picking: { isActive: boolean } } };
    const {
      model,
      coordinateConversion,
      bounds,
      disablePicking,
      redTexture,
      greenTexture,
      blueTexture
    } = this.state as unknown as ZarrCompositeBitmapLayerState;
    const {
      redRange,
      greenRange,
      blueRange,
      desaturate,
      transparentColor,
      tintColor
    } = this.props as unknown as ZarrCompositeBitmapLayerProps & {
      desaturate: number;
      transparentColor: [number, number, number, number];
      tintColor: [number, number, number];
    };

    if (shaderModuleProps.picking.isActive && disablePicking) {
      return;
    }
    if (!redTexture || !greenTexture || !blueTexture || !model) {
      return;
    }

    model.shaderInputs.setProps({
      bitmap: {
        bitmapTexture: redTexture,
        bounds,
        coordinateConversion,
        desaturate,
        tintColor: tintColor.slice(0, 3).map((value) => value / 255),
        transparentColor: transparentColor.map((value) => value / 255)
      },
      zarrComposite: {
        greenTexture,
        blueTexture,
        redRange,
        greenRange,
        blueRange
      }
    });
    model.draw(this.context.renderPass);
  }

  private replaceCompositeTextures(
    props: ZarrCompositeBitmapLayerProps,
    textureParameters: SamplerProps | null | undefined
  ) {
    this.destroyCompositeTextures();
    if (!props.redValues || !props.greenValues || !props.blueValues) {
      this.setState({ redTexture: null, greenTexture: null, blueTexture: null });
      return;
    }

    this.setState({
      redTexture: this.createScalarTexture("red", props.redValues, textureParameters),
      greenTexture: this.createScalarTexture("green", props.greenValues, textureParameters),
      blueTexture: this.createScalarTexture("blue", props.blueValues, textureParameters)
    });
  }

  private createScalarTexture(
    channel: string,
    rawValues: RawScalarTextureSource,
    textureParameters: SamplerProps | null | undefined
  ): Texture {
    return this.context.device.createTexture({
      id: `${this.props.id}:${channel}-values`,
      data: rawValues.data,
      width: rawValues.width,
      height: rawValues.height,
      format: "r32float",
      mipLevels: 1,
      sampler: {
        addressModeU: "clamp-to-edge",
        addressModeV: "clamp-to-edge",
        minFilter: "nearest",
        magFilter: "nearest",
        mipmapFilter: "nearest",
        ...textureParameters
      }
    });
  }

  private destroyCompositeTextures() {
    const { redTexture, greenTexture, blueTexture } = this.state as unknown as ZarrCompositeBitmapLayerState;
    redTexture?.destroy();
    greenTexture?.destroy();
    blueTexture?.destroy();
  }
}
