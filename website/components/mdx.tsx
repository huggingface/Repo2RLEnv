import { TutorialIndex } from '@/components/tutorials';
import defaultMdxComponents from 'fumadocs-ui/mdx';
import type { MDXComponents } from 'mdx/types';
import { Mermaid } from '@/components/mermaid';
import { Film } from '@/components/film';
import { Step, Steps } from 'fumadocs-ui/components/steps';
import { Tab, Tabs } from 'fumadocs-ui/components/tabs';
import { File, Files, Folder } from 'fumadocs-ui/components/files';
import { Accordion, Accordions } from 'fumadocs-ui/components/accordion';
import { TypeTable } from 'fumadocs-ui/components/type-table';
import { PipelineCatalog, TasksmithFeature } from '@/components/pipeline-catalog';

export function getMDXComponents(components?: MDXComponents) {
  return {
    ...defaultMdxComponents,
    Mermaid,
    Film,
    Step,
    Steps,
    Tab,
    Tabs,
    File,
    Files,
    Folder,
    Accordion,
    Accordions,
    TypeTable,
    PipelineCatalog,
    TasksmithFeature,
    TutorialIndex,
    ...components,
  } satisfies MDXComponents;
}

export const useMDXComponents = getMDXComponents;

declare global {
  type MDXProvidedComponents = ReturnType<typeof getMDXComponents>;
}
