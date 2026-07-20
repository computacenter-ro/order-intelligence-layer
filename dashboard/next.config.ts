import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // @computacenter-ro/style-guide ships some subpaths (e.g. /tokens) as raw
  // .ts source rather than compiled JS — Next needs to transpile it itself.
  transpilePackages: ["@computacenter-ro/style-guide"],
};

export default nextConfig;
