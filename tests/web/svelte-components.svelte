<script lang="ts">
  import {
    Alert,
    Badge,
    Button,
    Card,
    CardContent,
    CardDescription,
    CardFooter,
    CardHeader,
    CardTitle,
    Input,
    LinkButton,
  } from "@harnessa/ui-svelte";
</script>

<Alert variant="destructive">Unavailable</Alert>
<Badge variant="success">READY</Badge>
<Button variant="outline" size="sm">Save</Button>
<Card>
  <CardHeader><CardTitle>Workspace</CardTitle><CardDescription>Repository scope</CardDescription></CardHeader>
  <CardContent><Input aria-label="Workspace name" placeholder="Name" /></CardContent>
  <CardFooter>Review required</CardFooter>
</Card>
<LinkButton href="/projects" variant="secondary">Projects</LinkButton>
