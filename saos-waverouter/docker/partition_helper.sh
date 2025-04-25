FILTER='^loop'
lsblk --raw -a --output "NAME,MAJ:MIN" --noheadings | grep -E "$FILTER" | while read LINE; do
    DEV=/dev/$(echo $LINE | cut -d' ' -f1)
    MAJMIN=$(echo $LINE | cut -d' ' -f2)
    MAJ=$(echo $MAJMIN | cut -d: -f1)
    MIN=$(echo $MAJMIN | cut -d: -f2)
    [ -b "$DEV" ] || mknod "$DEV" b $MAJ $MIN
done