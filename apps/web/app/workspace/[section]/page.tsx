import { Workspace } from "@/components/workspace";

export default async function WorkspacePage({ params }: { params: Promise<{ section: string }> }) {
  const { section } = await params;
  return <Workspace section={section} />;
}
